"""频率作业生成端(F14)——打通 ΔE→论文级 ΔG 的第一缺口。

thermo.py 已有**解析端**(parse_freq_outcar/harmonic_thermo);本模块补齐**生成端**:
从完成的弛豫作业目录一键派生 VASP 频率作业(IBRION=5 有限差分),只放开吸附质及其
近邻表面原子(Selective dynamics 冻结其余),既拿到体系专属 ZPE/熵,又把算时压到可控。

设计原则(与生成区一致):
- **只改必须改的**:频率 INCAR 从弛豫 INCAR 派生,仅替换离子学关键键(IBRION/NFREE/
  POTIM/NSW/ISYM)、收紧 EDIFF、剥离 ISIF/EDIFFG;电子学参数(ENCUT/GGA/ISPIN/
  MAGMOM/IVDW/LDAU*)**原样保留**,逐条注明改动(输出含 changes 清单 + INCAR 注释)。
- **绝不静默猜**:吸附质识别不确定(无明显 z 间隙 / 顶部含非吸附质元素)时,结果附
  中文 warning,或在完全无法判定时显式 raise —— 交调用方决策,不静默生成错误自由原子集。
- 纯函数为主,复用 poscar/structure_view 既有解析,不另造 parser。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
import shutil
from collections import OrderedDict
from pathlib import Path

from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.generate.structure_view import parse_positions
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.kpoints import kpoints_str
from vcstudio.shared import manifest as manifest_mod

# 常见吸附质元素白名单(Li-S / 电催化体系):金属/骨架元素多不在此列,作识别辅助判据。
ADSORBATE_WHITELIST = frozenset({'Li', 'S', 'H', 'O', 'C', 'N'})
# 吸附质-表面最小 z 间隙(Å):最大相邻 z 间隙低于此值 → 无法可靠分离,判识别不可靠。
ADSORBATE_MIN_GAP = 1.0
SELECT_MODES = ('adsorbate_and_neighbors', 'adsorbate_only', 'all', 'explicit')


# ── 周期最小镜像几何(自足,不跨模块引私有名) ──────────────────────────────────
def _inv3x3(m: list) -> list:
    """3×3 矩阵求逆(纯 Python;与 slab_builder 同口径)。奇异 → ValueError。"""
    (a, b, c), (d, e, f), (g, h, i) = m[0], m[1], m[2]
    A = e * i - f * h
    B = f * g - d * i
    C = d * h - e * g
    det = a * A + b * B + c * C
    if abs(det) < 1e-12:
        raise ValueError('晶格矢量退化(行列式≈0),无法求周期最小镜像')
    inv = 1.0 / det
    return [
        [A * inv, (c * h - b * i) * inv, (b * f - c * e) * inv],
        [B * inv, (a * i - c * g) * inv, (c * d - a * f) * inv],
        [C * inv, (b * g - a * h) * inv, (a * e - b * d) * inv],
    ]


def _min_image_dist(fi: list, fj: list, cell: list) -> float:
    """两分数坐标间周期最小镜像距离(Å)。wrap 到 [−0.5,0.5) 后再搜 ±1 邻域像(斜胞也真最小)。"""
    df = [fi[k] - fj[k] for k in range(3)]
    df = [d - math.floor(d + 0.5) for d in df]
    a_vec, b_vec, c_vec = cell[0], cell[1], cell[2]
    best2 = float('inf')
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


def _fractional(coords: list, cell: list) -> list:
    """笛卡尔坐标 → 分数坐标(frac = cart · cell⁻¹;cell 行为晶格矢量)。"""
    inv = _inv3x3(cell)
    return [[c[0] * inv[0][j] + c[1] * inv[1][j] + c[2] * inv[2][j] for j in range(3)]
            for c in coords]


def _unwrap_z(coords: list, cell: list) -> list:
    """跨 z 边界回卷展开(CONTCAR 常态):圆周最大 z 间隙作切口,切口以下整体 +Lz。

    仅返回展开后 z 值列表(x/y 不动)。Lz 无效或 n<2 时返回原始 z。用于吸附质 z-间隙分离,
    防 slab 底层弛豫越界回卷被误当"最高原子"。
    """
    n = len(coords)
    lz = cell[2][2] if len(cell) > 2 and len(cell[2]) > 2 else 0.0
    zs = [c[2] for c in coords]
    if n < 2 or lz <= 0:
        return zs
    zmod = [((z % lz) + lz) % lz for z in zs]
    order = sorted(range(n), key=lambda i: zmod[i])
    best_k, best_gap = n - 1, zmod[order[0]] + lz - zmod[order[-1]]
    for k in range(n - 1):
        g = zmod[order[k + 1]] - zmod[order[k]]
        if g > best_gap:
            best_gap, best_k = g, k
    if best_k == n - 1:
        return zmod                       # 最大间隙恰跨边界 → 结构本连续
    low = set(order[:best_k + 1])
    return [zmod[i] + (lz if i in low else 0.0) for i in range(n)]


# ── 吸附质识别 ──────────────────────────────────────────────────────────────────
def detect_adsorbate(poscar_text: str, *, adsorbate_indices=None,
                     whitelist=None) -> dict:
    """识别吸附质原子索引 → ``{'indices','warnings','confident','method'}``。

    - 显式给 ``adsorbate_indices`` → 直接采用(confident=True,无 warning),越界 raise。
    - 否则启发:z 展开后取**最大相邻 z 间隙**上方原子团为吸附质候选(物理上吸附质悬于
      表面之上)。置信判据:间隙 ≥ ADSORBATE_MIN_GAP、候选元素全在白名单、候选不过半;
      任一不满足 → confident=False 且附中文 warning(请显式指定),**绝不静默当真**。
    - 完全无候选 / 体系 <2 原子 → raise ValueError(交人工)。
    """
    wl = frozenset(whitelist) if whitelist is not None else ADSORBATE_WHITELIST
    p = parse_positions(poscar_text)
    elements, coords, cell = p['elements'], p['coords'], p['cell']
    n = len(elements)

    if adsorbate_indices is not None:
        idx = sorted({int(i) for i in adsorbate_indices})
        if not idx:
            raise ValueError('adsorbate_indices 为空,请给出吸附质原子索引')
        if idx[0] < 0 or idx[-1] >= n:
            raise ValueError(f'adsorbate_indices 越界(体系 {n} 原子):{sorted(adsorbate_indices)}')
        return {'indices': idx, 'warnings': [], 'confident': True, 'method': 'explicit'}

    if n < 2:
        raise ValueError('体系少于 2 原子,无法区分吸附质与表面;请显式指定 adsorbate_indices')

    zs = _unwrap_z(coords, cell)
    order = sorted(range(n), key=lambda i: zs[i])
    gmax, kcut = -1.0, -1
    for k in range(n - 1):
        g = zs[order[k + 1]] - zs[order[k]]
        if g > gmax:
            gmax, kcut = g, k
    top = sorted(order[kcut + 1:]) if kcut >= 0 else []
    if not top:
        raise ValueError('无法识别吸附质:未找到顶部原子团,请显式指定 adsorbate_indices')

    warnings: list[str] = []
    confident = True
    if gmax < ADSORBATE_MIN_GAP:
        confident = False
        warnings.append(
            f'未检测到明显吸附质-表面 z 间隙(最大 {gmax:.2f} Å < {ADSORBATE_MIN_GAP} Å);'
            f'吸附质识别不可靠,请用 adsorbate_indices 显式指定自由原子。')
    non_wl = sorted({elements[i] for i in top if elements[i] not in wl})
    if non_wl:
        confident = False
        warnings.append(
            f'顶部候选含非常见吸附质元素 {non_wl},可能把表面原子误判为吸附质;'
            f'请核对或用 adsorbate_indices 显式指定。')
    if len(top) > n // 2:
        confident = False
        warnings.append(
            f'顶部候选占体系过半({len(top)}/{n}),疑似非 slab+吸附质构型;请显式指定。')
    return {'indices': top, 'warnings': warnings, 'confident': confident, 'method': 'z_gap'}


def _free_atoms_with_warnings(poscar_text: str, mode: str, adsorbate_indices,
                              cutoff: float):
    """内部:算自由原子索引 + 收集识别 warning。见 select_free_atoms 口径。"""
    p = parse_positions(poscar_text)
    n = len(p['elements'])
    if mode == 'all':
        return list(range(n)), []
    if mode == 'explicit':
        if not adsorbate_indices:
            raise ValueError("mode='explicit' 需显式提供 adsorbate_indices(即自由原子索引)")
        idx = sorted({int(i) for i in adsorbate_indices})
        if idx[0] < 0 or idx[-1] >= n:
            raise ValueError(f'explicit 自由原子索引越界(体系 {n} 原子):{sorted(adsorbate_indices)}')
        return idx, []
    if mode not in ('adsorbate_and_neighbors', 'adsorbate_only'):
        raise ValueError(f'未知 mode {mode!r}(可选:{SELECT_MODES})')

    det = detect_adsorbate(poscar_text, adsorbate_indices=adsorbate_indices)
    ads, warns = det['indices'], list(det['warnings'])
    if mode == 'adsorbate_only':
        return ads, warns
    # adsorbate_and_neighbors:并入 cutoff 内(周期最小镜像)的表面原子
    fracs = _fractional(p['coords'], p['cell'])
    neigh = []
    ads_set = set(ads)
    for i in range(n):
        if i in ads_set:
            continue
        di = min(_min_image_dist(fracs[i], fracs[j], p['cell']) for j in ads)
        if di <= cutoff:
            neigh.append(i)
    return sorted(ads_set | set(neigh)), warns


def select_free_atoms(poscar_text: str, mode: str = 'adsorbate_and_neighbors', *,
                      adsorbate_indices=None, cutoff: float = 3.0) -> list:
    """选出频率计算放开的自由原子索引(0 基)。

    mode:
    - ``'adsorbate_and_neighbors'``(默认):吸附质原子 + 距其 ``cutoff`` Å 内(周期最小
      镜像)表面原子;
    - ``'adsorbate_only'``:仅吸附质原子;
    - ``'all'``:全部原子(气相分子频率);
    - ``'explicit'``:自由原子 = ``adsorbate_indices`` 显式给定集合。

    吸附质识别见 detect_adsorbate:显式传 indices 最稳;启发识别不确定时其 warning 经
    build_freq_job 的 'warnings' 暴露(或用 detect_adsorbate 单独查),**绝不静默猜**。
    """
    return _free_atoms_with_warnings(poscar_text, mode, adsorbate_indices, cutoff)[0]


# ── Selective dynamics POSCAR ───────────────────────────────────────────────────
def build_freq_poscar(poscar_text: str, free_indices) -> str:
    """写频率 POSCAR:自由原子 ``T T T``,其余 ``F F F``(Selective dynamics)。

    - 已有 Selective dynamics 的只重写每原子标志(与 slab_builder.fix_bottom_layers 同口径);
      无则插入 'Selective dynamics' 行并给全部坐标行补三标志。Direct/Cartesian 均支持
      (坐标数值原样保留,只动标志),坐标块后尾行(速度块)透传。
    - 自由集为空 → ValueError(频率至少放开吸附质);索引越界 → ValueError。
    """
    syms, counts = parse_poscar_species(poscar_text)
    if not syms or not counts:
        raise ValueError('POSCAR 缺元素/计数行(VASP4 或畸形),无法写 Selective dynamics')
    natoms = sum(counts)
    free = {int(i) for i in free_indices}
    if not free:
        raise ValueError('自由原子集合为空:频率计算至少需放开吸附质原子')
    if min(free) < 0 or max(free) >= natoms:
        raise ValueError(f'自由原子索引越界(体系 {natoms} 原子):{sorted(free)}')

    lines = poscar_text.splitlines()
    has_sd = len(lines) > 7 and lines[7].strip()[:1].lower() == 's'
    mode_idx = 8 if has_sd else 7
    coord_start = mode_idx + 1

    out = list(lines[:7])                 # 注释/缩放/三矢量/元素/计数
    out.append('Selective dynamics')      # 规范化写入(替换旧行或新增)
    out.append(lines[mode_idx])           # Direct/Cartesian 模式行原样保留
    for k in range(natoms):
        parts = lines[coord_start + k].split()
        flags = 'T T T' if k in free else 'F F F'
        out.append(f'  {parts[0]} {parts[1]} {parts[2]}  {flags}')
    out.extend(lines[coord_start + natoms:])   # 尾部(速度块等)透传
    return '\n'.join(out) + '\n'


# ── 频率 INCAR 派生 ─────────────────────────────────────────────────────────────
_FREQ_SET = OrderedDict([                 # 必须替换的离子学关键键 → 频率取值
    ('ISTART', '0'), ('ICHARG', '2'),
    ('IBRION', '5'), ('NFREE', '2'), ('POTIM', '0.015'), ('NSW', '1'), ('ISYM', '0'),
])
_FREQ_STRIP = ('ISIF', 'EDIFFG')          # 频率无意义,剥离
_EDIFF_TARGET = '1E-07'                   # EDIFF 收紧至发文级(≤1e-7)
_FREQ_REASON = {
    'ISTART': '派生目录不复制 WAVECAR，从头初始化波函数',
    'ICHARG': '频率力常数必须自洽，且派生目录不复制 CHGCAR',
    'IBRION': '有限差分频率(Hessian)', 'NFREE': '两点有限差分', 'POTIM': '有限差分位移步长(Å)',
    'NSW': '单步(频率不做离子弛豫)', 'ISYM': '频率计算关对称,避免简并模式误分类',
    'EDIFF': '收紧电子收敛至发文级(≤1e-7)',
    'ISIF': '频率无需应力/晶胞优化', 'EDIFFG': '频率无离子弛豫收敛判据',
}


def _split_comment(line: str):
    """拆行为 (代码段, 注释段)。注释符取首个 '#' 或 '!'。"""
    idxs = [i for i in (line.find('#'), line.find('!')) if i != -1]
    if idxs:
        i = min(idxs)
        return line[:i], line[i:]
    return line, ''


def _ediff_action(base_parsed: dict):
    """EDIFF 处置:缺→add;>1e-7→tighten;≤1e-7→keep(原值)。"""
    v = base_parsed.get('EDIFF')
    if v is None:
        return 'add', None
    try:
        return ('keep' if float(v) <= 1e-7 else 'tighten'), v
    except (TypeError, ValueError):
        return 'tighten', v               # 非数字 EDIFF → 强制发文级


def _derive_freq_incar(base_incar_text: str):
    """弛豫 INCAR 文本 → (频率 INCAR 文本, changes 列表)。

    只改必须改的:替换 _FREQ_SET、剥离 _FREQ_STRIP、EDIFF 收紧;其余键(含所有电子学
    参数)逐字保留,顺序不动。changes 每项 {'key','action','old','new','reason'}。
    子句级处理 ';' 多赋值,保留行内注释。
    """
    parsed = parse_incar(base_incar_text)
    ediff_act, ediff_old = _ediff_action(parsed)
    changes: list[dict] = []
    handled: set[str] = set()
    new_lines: list[str] = []

    for line in base_incar_text.splitlines():
        code, comment = _split_comment(line)
        if '=' not in code:
            new_lines.append(line)        # 纯注释/空行/SYSTEM 头等原样
            continue
        kept = []
        for clause in code.split(';'):
            if '=' not in clause:
                if clause.strip():
                    kept.append(clause.strip())
                continue
            key = clause.split('=', 1)[0].strip().upper()
            if key in _FREQ_STRIP:
                changes.append({'key': key, 'action': 'strip', 'old': parsed.get(key),
                                'new': None, 'reason': _FREQ_REASON.get(key, '')})
                continue                  # 丢弃该子句
            if key in _FREQ_SET:
                new = _FREQ_SET[key]
                changes.append({'key': key, 'action': 'replace', 'old': parsed.get(key),
                                'new': new, 'reason': _FREQ_REASON.get(key, '')})
                kept.append(f'{key} = {new}')
                handled.add(key)
                continue
            if key == 'EDIFF' and ediff_act != 'keep':
                changes.append({'key': 'EDIFF', 'action': ediff_act, 'old': ediff_old,
                                'new': _EDIFF_TARGET, 'reason': _FREQ_REASON['EDIFF']})
                kept.append(f'EDIFF = {_EDIFF_TARGET}')
                handled.add('EDIFF')
                continue
            kept.append(clause.strip())   # 非目标键逐字保留
        if kept:
            merged = ' ; '.join(kept)
            new_lines.append(merged + (('  ' + comment) if comment else ''))
        # 整行子句全被剥离 → 连同注释丢弃

    # 补齐弛豫 INCAR 里缺席但频率必须的键
    for key, val in _FREQ_SET.items():
        if key not in handled:
            changes.append({'key': key, 'action': 'add', 'old': None, 'new': val,
                            'reason': _FREQ_REASON.get(key, '')})
            new_lines.append(f'{key} = {val}')
    if 'EDIFF' not in handled and ediff_act == 'add':
        changes.append({'key': 'EDIFF', 'action': 'add', 'old': None, 'new': _EDIFF_TARGET,
                        'reason': _FREQ_REASON['EDIFF']})
        new_lines.append(f'EDIFF = {_EDIFF_TARGET}')

    banner = _freq_incar_banner(changes)
    return banner + '\n'.join(new_lines) + '\n', changes


def _freq_incar_banner(changes: list) -> str:
    """把 changes 渲染为 INCAR 头部注释块(逐条注明,输出自带 changes 清单)。"""
    out = ['# === vcstudio 频率作业 INCAR(派生自弛豫 INCAR;只改频率必需项) ===']
    for c in changes:
        act, key = c['action'], c['key']
        if act in ('add',):
            out.append(f"#   新增 {key} = {c['new']}  ({c['reason']})")
        elif act in ('replace', 'tighten'):
            out.append(f"#   {key}: {c['old']} -> {c['new']}  ({c['reason']})")
        elif act == 'strip':
            out.append(f"#   剥离 {key}(原 {c['old']}):{c['reason']}")
    out.append('# 电子学参数(ENCUT/GGA/ISPIN/MAGMOM/IVDW/LDAU*)原样保留。')
    return '\n'.join(out) + '\n'


def build_freq_incar(base_incar_text: str) -> str:
    """弛豫 INCAR 文本 → 频率 INCAR 文本(含改动注释块)。结构化 changes 见 build_freq_job。

    派生规则:显式从头自洽(ISTART=0/ICHARG=2)，替换 IBRION=5 / NFREE=2 /
    POTIM=0.015 / NSW=1 / ISYM=0;EDIFF 收紧至 ≤1e-7;剥离 ISIF / EDIFFG;
    其余电子学参数逐字保留。
    """
    return _derive_freq_incar(base_incar_text)[0]


# ── 一键派生频率作业目录 ───────────────────────────────────────────────────────
def _read_text(path: str):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _gamma_kpoints() -> str:
    """Γ 点单点 KPOINTS(1×1×1;气相分子/大胞频率常用)。"""
    return kpoints_str([1, 1, 1])


def _structure_source(relax_dir: str):
    """取弛豫末构型 → (来源名, 文本)。优先**非空** CONTCAR(避免 VASP 空 CONTCAR 陷阱),
    否则退 POSCAR;都无 → (None, None)。"""
    contcar = _read_text(os.path.join(relax_dir, 'CONTCAR'))
    if contcar is not None and contcar.strip():
        return 'CONTCAR', contcar
    poscar = _read_text(os.path.join(relax_dir, 'POSCAR'))
    if poscar is not None and poscar.strip():
        return 'POSCAR', poscar
    return None, None


def _write_freq_job(relax_dir: str, out_dir: str, poscar_text: str, source_name: str,
                    free: list, warns: list, calc_type: str, kpoints: str) -> dict:
    """内部:落频率作业四件套 + job.yaml(task_type='freq',记溯源)。返回结果 dict。"""
    base_incar = _read_text(os.path.join(relax_dir, 'INCAR'))
    if base_incar is None:
        raise ValueError(f'弛豫目录缺 INCAR,无法派生频率作业:{relax_dir}')

    freq_poscar = build_freq_poscar(poscar_text, free)
    freq_incar, changes = _derive_freq_incar(base_incar)
    warnings = list(warns)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
        f.write(freq_poscar)
    with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(freq_incar)

    # KPOINTS:'original' 复制原网格(缺失则退 Γ 并告警);'gamma' 直接写 Γ 单点
    src_kpoints = os.path.join(relax_dir, 'KPOINTS')
    if kpoints == 'gamma':
        with open(os.path.join(out_dir, 'KPOINTS'), 'w', encoding='utf-8') as f:
            f.write(_gamma_kpoints())
    elif os.path.isfile(src_kpoints):
        shutil.copyfile(src_kpoints, os.path.join(out_dir, 'KPOINTS'))
    else:
        with open(os.path.join(out_dir, 'KPOINTS'), 'w', encoding='utf-8') as f:
            f.write(_gamma_kpoints())
        warnings.append('弛豫目录缺 KPOINTS,已退化为 Γ 点单点网格(请核对是否足够)。')

    # POTCAR 原样复制(赝势身份不得变)
    src_potcar = os.path.join(relax_dir, 'POTCAR')
    if os.path.isfile(src_potcar):
        shutil.copyfile(src_potcar, os.path.join(out_dir, 'POTCAR'))
    else:
        warnings.append('弛豫目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')

    _save_freq_manifest(out_dir, relax_dir, poscar_text, source_name,
                        free, changes, warnings, calc_type)
    return {'out_dir': str(out_dir), 'free_atoms': free, 'changes': changes,
            'warnings': warnings}


def _save_freq_manifest(out_dir, relax_dir, poscar_text, source_name,
                        free, changes, warnings, calc_type):
    """写 job.yaml:task_type='freq',记 parent_job / free_indices / incar_changes 溯源。"""
    syms, _counts = parse_poscar_species(poscar_text)
    system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_dir).name
    parent = str(Path(relax_dir).resolve())
    inputs = {
        'parent_job': parent,
        'derived_from': source_name,               # CONTCAR / POSCAR
        'free_indices': list(free),
        'n_free': len(free),
        'incar_changes': changes,
        'elements': list(syms),
    }
    m = manifest_mod.new_manifest(
        job_id=f'{Path(out_dir).name}-freq', system=system, task_type='freq',
        calc_type=calc_type, inputs=inputs, warnings=warnings)
    m['parent_job'] = parent                        # 顶层冗余一份,便于快速溯源
    manifest_mod.save_manifest(out_dir, m)
    return m


def build_freq_job(relax_dir, out_dir, *, mode: str = 'adsorbate_and_neighbors',
                   adsorbate_indices=None, cutoff: float = 3.0,
                   kpoints: str = 'original') -> dict:
    """从完成的弛豫作业目录一键派生频率作业目录(F14)。

    读 relax_dir 的 CONTCAR(缺则 POSCAR)+ INCAR + KPOINTS + POTCAR,生成:
    CONTCAR→POSCAR(Selective dynamics,只放开自由原子)+ 派生 INCAR(见 build_freq_incar)
    + KPOINTS(``kpoints='original'`` 复制原网格 / ``'gamma'`` Γ 单点)+ POTCAR 原样;
    写 job.yaml(task_type='freq',记 parent_job 与 free_indices/incar_changes 溯源)。

    free 原子由 select_free_atoms(mode/adsorbate_indices/cutoff)决定;吸附质识别不确定
    的 warning 汇入返回的 'warnings'(不静默)。
    Returns: ``{'out_dir','free_atoms','changes','warnings'}``。
    """
    relax_dir = str(relax_dir)
    source_name, poscar_text = _structure_source(relax_dir)
    if poscar_text is None:
        raise ValueError(f'弛豫目录缺 CONTCAR/POSCAR(或均为空),无法派生频率作业:{relax_dir}')
    free, warns = _free_atoms_with_warnings(poscar_text, mode, adsorbate_indices, cutoff)
    return _write_freq_job(relax_dir, out_dir, poscar_text, source_name, free, warns,
                           calc_type='slab', kpoints=kpoints)


def build_molecule_freq_job(relax_dir, out_dir, *, kpoints: str = 'gamma') -> dict:
    """气相分子频率作业:全原子放开(T T T),ISYM=0(派生 INCAR 已含),默认 Γ 单点。

    Returns: ``{'out_dir','free_atoms','changes','warnings'}``(free_atoms=全原子)。
    """
    relax_dir = str(relax_dir)
    source_name, poscar_text = _structure_source(relax_dir)
    if poscar_text is None:
        raise ValueError(f'弛豫目录缺 CONTCAR/POSCAR(或均为空),无法派生频率作业:{relax_dir}')
    free, warns = _free_atoms_with_warnings(poscar_text, 'all', None, 0.0)
    return _write_freq_job(relax_dir, out_dir, poscar_text, source_name, free, warns,
                           calc_type='molecule', kpoints=kpoints)
