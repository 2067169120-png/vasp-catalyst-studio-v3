"""收敛扫描器(欠账 P0):ENCUT / k 网格 / 真空 / 层厚 四类收敛系列的派生 + 解析 + 出图。

发文前置校验的第一环:任何 slab/bulk 计算,ENCUT 与 k 网格必须做收敛测试(审稿常问),
真空层与层厚决定 slab 能量是否收敛到孤立表面极限。本模块把「派生一串只改单一参数的作业 →
解析各作业末态能量 → 判收敛点 → 出收敛曲线」做成闭环。

设计原则(与 freq/aimd 生成端一致):
- **固定几何单点**:所有系列先把母 INCAR 规范成自洽静态(NSW=0/IBRION=-1，清掉
  ISIF/EDIFFG 和续算态)，同一系列再只改变目标参数。这样不会把不同点各自重新弛豫后
  的结构差误算成 ENCUT/k 点/真空收敛效应。
- **绝不静默**:层厚系列需重建 slab(裸 CONTCAR 不含体相/米勒面信息无法再生),不可再生时
  显式返回说明而非编造结构;真空系列假设 c⊥ab 沿 z,倾斜胞附 warning。
- 纯函数为主,复用 poscar/structure_view/kpoints 既有解析与 cluster.convergence 的 OSZICAR 解析。

本模块另提供**通用 INCAR 派生助手** ``derive_incar``(cell_opt / eos 复用):替换/新增/剥离指定键,
其余键逐字保留、逐条 changes 留痕(与 _derive_freq_incar/_derive_aimd_incar 同口径,单点实现)。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import shutil
from collections import OrderedDict
from pathlib import Path

from vcstudio.cluster.convergence import parse_oszicar
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.kpoints import kpoints_str
from vcstudio.generate.poscar import parse_poscar_species, read_cell_vectors
from vcstudio.generate.slab_builder import vacuum_thickness
from vcstudio.generate.structure_view import parse_positions
from vcstudio.shared import manifest as manifest_mod

DEFAULT_ENCUT_VALUES = (400, 450, 500, 550, 600, 650)
DEFAULT_THRESHOLD_MEV = 1.0        # 相邻收敛判据:1 meV/atom

_CONV_STATIC_SET = OrderedDict([
    ('ISTART', 0), ('ICHARG', 2), ('IBRION', -1), ('NSW', 0),
])
_CONV_STATIC_STRIP = ('ISIF', 'EDIFFG')
_CONV_STATIC_REASONS = {
    'ISTART': '收敛扫描目录不依赖母作业 WAVECAR',
    'ICHARG': '每个扫描点从原子叠加电荷独立自洽',
    'IBRION': '收敛扫描采用固定几何静态单点',
    'NSW': '收敛扫描不做离子步',
    'ISIF': '固定几何单点不做应力/变胞',
    'EDIFFG': '固定几何单点无离子收敛判据',
}


# ── 通用小工具(部分供 cell_opt / eos 复用) ─────────────────────────────────────
def _read_text(path: str):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _structure_source(src_dir: str):
    """取末构型 → (来源名, 文本)。优先**非空** CONTCAR,否则退 POSCAR;都无 → (None, None)。"""
    contcar = _read_text(os.path.join(src_dir, 'CONTCAR'))
    if contcar is not None and contcar.strip():
        return 'CONTCAR', contcar
    poscar = _read_text(os.path.join(src_dir, 'POSCAR'))
    if poscar is not None and poscar.strip():
        return 'POSCAR', poscar
    return None, None


def _split_comment(line: str):
    """拆行为 (代码段, 注释段)。注释符取首个 '#' 或 '!'(与 freq/aimd 同口径)。"""
    idxs = [i for i in (line.find('#'), line.find('!')) if i != -1]
    if idxs:
        i = min(idxs)
        return line[:i], line[i:]
    return line, ''


def derive_incar(base_incar_text: str, *, set_keys=None, strip_keys=(), reasons=None,
                 banner: str | None = None):
    """通用 INCAR 派生:替换/新增 set_keys、剥离 strip_keys,其余键逐字保留(顺序不动)。

    与 _derive_freq_incar/_derive_aimd_incar 同口径的**单点实现**(供本模块 + cell_opt + eos
    复用):子句级处理 ';' 多赋值、保留行内注释;set_keys/strip_keys/reasons 的键统一大写。

    Returns:
        ``(新 INCAR 文本, changes)``;changes 每项 ``{'key','action','old','new','reason'}``,
        action ∈ replace/add/strip。banner 非空则作头部注释块。
    """
    setk = OrderedDict((str(k).upper(), v) for k, v in (set_keys or {}).items())
    strip = {str(k).upper() for k in strip_keys}
    rs = {str(k).upper(): v for k, v in (reasons or {}).items()}
    parsed = parse_incar(base_incar_text)
    changes: list[dict] = []
    handled: set[str] = set()
    new_lines: list[str] = []

    for line in base_incar_text.splitlines():
        code, comment = _split_comment(line)
        if '=' not in code:
            new_lines.append(line)              # 纯注释/空行/SYSTEM 头等原样
            continue
        kept = []
        for clause in code.split(';'):
            if '=' not in clause:
                if clause.strip():
                    kept.append(clause.strip())
                continue
            key = clause.split('=', 1)[0].strip().upper()
            if key in strip:
                changes.append({'key': key, 'action': 'strip', 'old': parsed.get(key),
                                'new': None, 'reason': rs.get(key, '')})
                continue
            if key in setk:
                new = setk[key]
                changes.append({'key': key, 'action': 'replace', 'old': parsed.get(key),
                                'new': new, 'reason': rs.get(key, '')})
                kept.append(f'{key} = {new}')
                handled.add(key)
                continue
            kept.append(clause.strip())         # 非目标键逐字保留
        if kept:
            merged = ' ; '.join(kept)
            new_lines.append(merged + (('  ' + comment) if comment else ''))

    for key, val in setk.items():
        if key not in handled:
            changes.append({'key': key, 'action': 'add', 'old': None, 'new': val,
                            'reason': rs.get(key, '')})
            new_lines.append(f'{key} = {val}')

    head = (banner + '\n') if banner else ''
    return head + '\n'.join(new_lines) + '\n', changes


def _derive_conv_static_incar(base_incar_text: str, *, set_keys=None,
                              reasons=None, banner=None):
    """把母作业统一派生为固定几何自洽单点，再叠加本系列唯一目标参数。"""
    merged = OrderedDict(_CONV_STATIC_SET)
    merged.update((str(k).upper(), v) for k, v in (set_keys or {}).items())
    why = dict(_CONV_STATIC_REASONS)
    why.update({str(k).upper(): v for k, v in (reasons or {}).items()})
    return derive_incar(
        base_incar_text, set_keys=merged, strip_keys=_CONV_STATIC_STRIP,
        reasons=why, banner=banner)


def _det3(m) -> float:
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def _cell_volume(poscar_text: str) -> float:
    return abs(_det3(read_cell_vectors(poscar_text)))


def set_vacuum(poscar_text: str, vacuum: float) -> tuple:
    """把 slab POSCAR 的真空层设为 ``vacuum`` Å(只改 c 长度、slab 沿 z 居中,原子笛卡尔不缩放)。

    假设 c⊥ab 沿 z(slab 惯例):新 |c| = (z_max−z_min) + vacuum,原子整体平移使 z_min=vacuum/2。
    Direct/Cartesian 输入均可(内部转笛卡尔重写)。返回 ``(新 POSCAR 文本, warnings)``;
    c 有明显面内分量(倾斜胞)时附 warning(该情形应显式重建,勿盲用)。
    """
    p = parse_positions(poscar_text)
    coords, cell = p['coords'], p['cell']
    syms, counts = parse_poscar_species(poscar_text)
    if not syms or not counts:
        raise ValueError('POSCAR 缺元素/计数行(VASP4 或畸形),无法重建真空层')
    warnings: list[str] = []
    c_len = (cell[2][0] ** 2 + cell[2][1] ** 2 + cell[2][2] ** 2) ** 0.5
    if c_len > 0 and (abs(cell[2][0]) + abs(cell[2][1])) / c_len > 0.05:
        warnings.append('c 矢量有明显面内分量(非 c⊥ab 沿 z):真空系列按 z 方向重建可能不准确,'
                        '请对倾斜胞显式重建 slab。')
    zs = [c[2] for c in coords]
    z_min = min(zs) if zs else 0.0
    span = (max(zs) - z_min) if zs else 0.0
    shift = float(vacuum) / 2.0 - z_min
    new_coords = [[c[0], c[1], c[2] + shift] for c in coords]
    new_c = span + float(vacuum)

    comment = poscar_text.splitlines()[0]
    out = [comment, '1.0',
           f'  {cell[0][0]:.10f} {cell[0][1]:.10f} {cell[0][2]:.10f}',
           f'  {cell[1][0]:.10f} {cell[1][1]:.10f} {cell[1][2]:.10f}',
           f'  0.0000000000 0.0000000000 {new_c:.10f}',
           '  ' + ' '.join(syms), '  ' + ' '.join(str(c) for c in counts),
           'Cartesian']
    for c in new_coords:
        out.append(f'  {c[0]:.10f} {c[1]:.10f} {c[2]:.10f}')
    return '\n'.join(out) + '\n', warnings


# ── 系列派生(逐作业只改单一目标参数) ──────────────────────────────────────────
def _copy_if(src_dir, out_dir, name):
    """存在则复制 src_dir/name 到 out_dir/name,返回是否复制。"""
    src = os.path.join(src_dir, name)
    if os.path.isfile(src):
        shutil.copyfile(src, os.path.join(out_dir, name))
        return True
    return False


def _save_conv_manifest(out_dir, src_dir, poscar_text, series, series_value,
                        series_label, changes, warnings):
    """写 job.yaml:task_type='conv_scan',记 parent_job / 系列名·值·标签 / changes 溯源。"""
    syms, counts = parse_poscar_species(poscar_text)
    system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_dir).name
    parent = str(Path(src_dir).resolve())
    inputs = {
        'parent_job': parent, 'series': series,
        'series_value': series_value, 'series_label': series_label,
        'natoms': int(sum(counts)) if counts else None,
        'incar_changes': changes, 'elements': list(syms),
    }
    inputs['sha256'] = {
        name: manifest_mod.sha256_file(Path(out_dir) / name)
        for name in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR')
        if (Path(out_dir) / name).is_file()
    }
    m = manifest_mod.new_manifest(
        job_id=f'{Path(out_dir).name}-conv', system=system, task_type='conv_scan',
        calc_type='slab', inputs=inputs, warnings=warnings)
    m['parent_job'] = parent
    manifest_mod.save_manifest(out_dir, m)
    return m


def _require_inputs(src_dir):
    """取 (末构型来源, POSCAR 文本, INCAR 文本);缺任一 → ValueError。"""
    source_name, poscar_text = _structure_source(src_dir)
    if poscar_text is None:
        raise ValueError(f'源目录缺 CONTCAR/POSCAR(或均为空),无法派生收敛系列:{src_dir}')
    incar_text = _read_text(os.path.join(src_dir, 'INCAR'))
    if incar_text is None:
        raise ValueError(f'源目录缺 INCAR,无法派生收敛系列:{src_dir}')
    return source_name, poscar_text, incar_text


def build_encut_series(src_dir, out_root, values=DEFAULT_ENCUT_VALUES) -> dict:
    """ENCUT 收敛系列:固定几何静态基线，各作业只改变 ENCUT。

    作业目录名 ``encut_<值>``。返回 ``{'out_root','dirs','series','results','warnings'}``,
    series=[{'value','label','dir'}, ...](供 analyze_series/conv_plot 消费)。
    """
    src_dir = str(src_dir)
    _src, poscar_text, incar_text = _require_inputs(src_dir)
    dirs, series, results = OrderedDict(), [], OrderedDict()
    top_warn: list[str] = []
    for v in values:
        v = int(v)
        out_dir = os.path.join(out_root, f'encut_{v}')
        os.makedirs(out_dir, exist_ok=True)
        new_incar, changes = _derive_conv_static_incar(
            incar_text, set_keys={'ENCUT': v}, reasons={'ENCUT': 'ENCUT 收敛扫描目标值(eV)'},
            banner=f'# === vcstudio ENCUT 收敛系列(固定几何静态;ENCUT={v}) ===')
        warnings: list[str] = []
        with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
            f.write(poscar_text)
        with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
            f.write(new_incar)
        if not _copy_if(src_dir, out_dir, 'KPOINTS'):
            warnings.append('源目录缺 KPOINTS,未复制;收敛系列须各作业同一 k 网格,请补齐。')
        if not _copy_if(src_dir, out_dir, 'POTCAR'):
            warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')
        _save_conv_manifest(out_dir, src_dir, poscar_text, 'encut', v, f'{v} eV',
                            changes, warnings)
        dirs[v] = out_dir
        series.append({'value': v, 'label': f'{v} eV', 'dir': out_dir})
        results[v] = {'dir': out_dir, 'changes': changes, 'warnings': warnings}
    return {'out_root': str(out_root), 'dirs': dirs, 'series': series,
            'results': results, 'warnings': top_warn}


def build_kmesh_series(src_dir, out_root, meshes) -> dict:
    """k 网格收敛系列:固定几何静态基线，各作业只改变 KPOINTS。

    meshes:``[[kx,ky,kz], ...]``。作业目录名 ``kmesh_<kx>x<ky>x<kz>``;series_value 取
    k 点积(kx·ky·kz,单调代理供收敛排序),series_label 为 'kx×ky×kz'。
    """
    src_dir = str(src_dir)
    _src, poscar_text, incar_text = _require_inputs(src_dir)
    dirs, series, results = OrderedDict(), [], OrderedDict()
    for mesh in meshes:
        kx, ky, kz = (int(mesh[0]), int(mesh[1]), int(mesh[2]))
        nk = kx * ky * kz
        label = f'{kx}×{ky}×{kz}'
        out_dir = os.path.join(out_root, f'kmesh_{kx}x{ky}x{kz}')
        os.makedirs(out_dir, exist_ok=True)
        warnings: list[str] = []
        new_incar, incar_changes = _derive_conv_static_incar(
            incar_text,
            banner='# === vcstudio k 网格收敛系列(固定几何静态) ===')
        with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
            f.write(poscar_text)
        with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
            f.write(new_incar)
        with open(os.path.join(out_dir, 'KPOINTS'), 'w', encoding='utf-8') as f:
            f.write(kpoints_str([kx, ky, kz]))
        if not _copy_if(src_dir, out_dir, 'POTCAR'):
            warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')
        changes = list(incar_changes)
        changes.append(f'KPOINTS: → {label}(Gamma-centered;k 网格收敛扫描目标)')
        _save_conv_manifest(out_dir, src_dir, poscar_text, 'kmesh', nk, label,
                            changes, warnings)
        dirs[nk] = out_dir
        series.append({'value': nk, 'label': label, 'dir': out_dir, 'mesh': [kx, ky, kz]})
        results[nk] = {'dir': out_dir, 'changes': changes, 'warnings': warnings}
    return {'out_root': str(out_root), 'dirs': dirs, 'series': series,
            'results': results, 'warnings': []}


def build_vacuum_series(src_dir, out_root, vacuums) -> dict:
    """真空层收敛系列:固定几何静态基线，各作业只改变 POSCAR 的 c 真空。

    vacuums:目标真空厚度列表(Å)。作业目录名 ``vac_<值>``(值取整或一位小数)。
    slab 沿 z 居中重建(见 set_vacuum),倾斜胞附 warning。
    """
    src_dir = str(src_dir)
    _src, poscar_text, incar_text = _require_inputs(src_dir)
    try:
        old_vac = vacuum_thickness(poscar_text)
    except (ValueError, NotImplementedError, IndexError):
        old_vac = None
    dirs, series, results = OrderedDict(), [], OrderedDict()
    for vac in vacuums:
        vac = float(vac)
        tag = f'{vac:g}'
        out_dir = os.path.join(out_root, f'vac_{tag}')
        os.makedirs(out_dir, exist_ok=True)
        new_poscar, warnings = set_vacuum(poscar_text, vac)
        new_incar, incar_changes = _derive_conv_static_incar(
            incar_text,
            banner='# === vcstudio 真空层收敛系列(固定几何静态) ===')
        with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
            f.write(new_poscar)
        with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
            f.write(new_incar)
        if not _copy_if(src_dir, out_dir, 'KPOINTS'):
            warnings.append('源目录缺 KPOINTS,未复制;收敛系列须各作业同一 k 网格,请补齐。')
        if not _copy_if(src_dir, out_dir, 'POTCAR'):
            warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')
        old_txt = f'{old_vac:.2f}' if old_vac is not None else '?'
        changes = list(incar_changes)
        changes.append(
            f'POSCAR c 真空层: {old_txt} → {vac:g} Å(真空收敛扫描目标;slab 沿 z 居中)')
        _save_conv_manifest(out_dir, src_dir, new_poscar, 'vacuum', vac, f'{vac:g} Å',
                            changes, warnings)
        dirs[vac] = out_dir
        series.append({'value': vac, 'label': f'{vac:g} Å', 'dir': out_dir})
        results[vac] = {'dir': out_dir, 'changes': changes, 'warnings': warnings}
    return {'out_root': str(out_root), 'dirs': dirs, 'series': series,
            'results': results, 'warnings': []}


def build_slab_thickness_series(src_dir, out_root, layers, *, slab_builder_fn=None) -> dict:
    """层厚收敛系列:固定几何静态基线，各作业改变 POSCAR 的 slab 层数。

    **层厚系列需重建 slab**:裸 CONTCAR 不含体相晶胞/米勒面/终止面信息,无法从已有 slab 反推
    更厚/更薄的 slab。故必须由调用方提供 ``slab_builder_fn(n_layers) -> POSCAR 文本``(源自 sac/
    slab 模板,知道如何再生);未提供则**不编造结构**,直接返回说明(note)与空结果。

    layers:目标层数列表(int)。作业目录名 ``nlayers_<n>``。
    """
    src_dir = str(src_dir)
    if slab_builder_fn is None:
        return {
            'out_root': str(out_root), 'dirs': OrderedDict(), 'series': [],
            'results': OrderedDict(), 'warnings': [],
            'note': ('层厚系列需重建不同层数的 slab,而裸 CONTCAR/POSCAR 不含体相晶胞与米勒面/'
                     '终止面信息,无法从已有 slab 再生。请用「结构建模页 → 金属 slab 建模」生成'
                     '基线作业(job.yaml 自带可再生配方,层厚收敛即可从该作业一键派生),或显式'
                     '提供 slab_builder_fn(n_layers)->POSCAR;本次未生成任何作业(不编造结构)。')}
    _src, _poscar, incar_text = _require_inputs(src_dir)
    dirs, series, results = OrderedDict(), [], OrderedDict()
    for n in layers:
        n = int(n)
        out_dir = os.path.join(out_root, f'nlayers_{n}')
        os.makedirs(out_dir, exist_ok=True)
        poscar_text = slab_builder_fn(n)
        warnings: list[str] = []
        new_incar, incar_changes = _derive_conv_static_incar(
            incar_text,
            banner='# === vcstudio slab 层厚收敛系列(固定几何静态) ===')
        with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
            f.write(poscar_text)
        with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
            f.write(new_incar)
        if not _copy_if(src_dir, out_dir, 'KPOINTS'):
            warnings.append('源目录缺 KPOINTS,未复制;层厚系列面内 k 网格须一致,请补齐。')
        if not _copy_if(src_dir, out_dir, 'POTCAR'):
            warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')
        changes = list(incar_changes)
        changes.append(f'POSCAR: 重建为 {n} 层 slab(层厚收敛扫描目标)')
        _save_conv_manifest(out_dir, src_dir, poscar_text, 'slab_thickness', n,
                            f'{n} 层', changes, warnings)
        dirs[n] = out_dir
        series.append({'value': n, 'label': f'{n} 层', 'dir': out_dir})
        results[n] = {'dir': out_dir, 'changes': changes, 'warnings': warnings}
    return {'out_root': str(out_root), 'dirs': dirs, 'series': series,
            'results': results, 'warnings': [], 'note': None}


# ── 系列解析 + 收敛判定 ──────────────────────────────────────────────────────────
def _final_energy(job_dir: str):
    """读已完成扫描点的末态 E0；有 manifest 时非 DONE 的中间能量一律不用。"""
    m = manifest_mod.load_manifest(job_dir)
    if m is not None and m.get('state') != 'DONE':
        return None
    txt = _read_text(os.path.join(str(job_dir), 'OSZICAR'))
    if txt is None:
        return None
    steps = parse_oszicar(txt)
    return steps[-1]['E0'] if steps else None


def _natoms_of(job_dir: str):
    """从作业目录 job.yaml(优先)或 POSCAR 取原子数;取不到 → None。"""
    m = manifest_mod.load_manifest(job_dir)
    if m and isinstance(m.get('inputs'), dict) and m['inputs'].get('natoms'):
        return int(m['inputs']['natoms'])
    txt = _read_text(os.path.join(str(job_dir), 'POSCAR'))
    if txt:
        _syms, counts = parse_poscar_species(txt)
        if counts:
            return int(sum(counts))
    return None


def analyze_series(dirs, *, natoms=None, threshold_mev: float = DEFAULT_THRESHOLD_MEV) -> dict:
    """解析收敛系列 → ``{'points','converged_at','threshold_mev','natoms','note'}``。

    dirs:``{x: 作业目录}`` 映射(推荐),或作业目录**列表**(x 从各 job.yaml 的 series_value 取)。
    points=[{'x','energy','converged'},...](按 x 升序);energy 只取 DONE 作业的 OSZICAR
    末态 E0(有 manifest 但未完成→None；无 manifest 的旧目录维持兼容)。
    收敛判据:相邻两点 |ΔE|/natoms < threshold_mev(默认 1 meV/atom)。converged_at=**首个**满足
    该判据的 x(即达到收敛的最小参数值);无一满足或有效点不足 → None + 中文 note。
    """
    # 归一化为 [(x, dir), ...]
    if isinstance(dirs, dict):
        items = list(dirs.items())
    else:
        items = []
        for d in dirs:
            m = manifest_mod.load_manifest(d)
            x = m['inputs'].get('series_value') if (m and isinstance(m.get('inputs'), dict)) else None
            items.append((x, d))
    items = [(x, d) for x, d in items if x is not None]
    items.sort(key=lambda t: t[0])

    if natoms is None and items:
        natoms = _natoms_of(items[0][1])
    thr_ev = float(threshold_mev) / 1000.0 * (natoms if natoms else 1)   # 每原子阈换算成总能阈

    points = []
    for x, d in items:
        points.append({'x': x, 'energy': _final_energy(d), 'converged': False})

    converged_at = None
    prev = None
    for pt in points:
        e = pt['energy']
        if e is None:
            prev = None
            continue
        if prev is not None and abs(e - prev) < thr_ev:
            pt['converged'] = True
            if converged_at is None:
                converged_at = pt['x']
        prev = e

    valid = [p for p in points if p['energy'] is not None]
    if not valid:
        note = '系列内无任一作业含可解析的 OSZICAR 能量(尚未运行?),无法判收敛。'
    elif natoms is None:
        note = (f'未能确定原子数,阈值按总能 {threshold_mev:g} meV 处理(非每原子);'
                + (f'收敛点 x={converged_at}。' if converged_at is not None else '尚未收敛。'))
    elif converged_at is not None:
        note = f'相邻能量差 < {threshold_mev:g} meV/atom(natoms={natoms})的最小 x = {converged_at}。'
    else:
        note = (f'系列内相邻能量差均 ≥ {threshold_mev:g} meV/atom(natoms={natoms}),'
                '尚未收敛,建议向更高参数继续扫描。')
    return {'points': points, 'converged_at': converged_at,
            'threshold_mev': float(threshold_mev), 'natoms': natoms, 'note': note}


def conv_plot(points, out_path, *, xlabel: str = 'parameter', converged_at=None,
              threshold_mev: float = DEFAULT_THRESHOLD_MEV, natoms=None,
              ylabel: str | None = None, title: str = '', width=None,
              palette: str = 'tol_bright', panel: str = '',
              formats=('png', 'pdf'), style_kw: dict | None = None) -> list:
    """收敛曲线:能量 vs 目标参数,标收敛点(金星+竖线)与收敛判据阈值带(±阈,绕收敛能)。

    points:analyze_series 的 'points'(需 ≥2 个 energy 非 None 的点)。纵轴默认相对末点
    能量的每原子偏差(meV/atom,直观看收敛),natoms 缺省用总能 meV。converged_at 给定则
    高亮该 x。样式走 native_charts.apply_paper_style。返回导出文件绝对路径列表。
    """
    from vcstudio.external.native_charts import (PALETTES, ZERO_LINE_COLOR, SINGLE_COL,
                                                 apply_paper_style, _new_figure,
                                                 _save_dual, add_panel_label)
    pts = [p for p in points if p.get('energy') is not None]
    if len(pts) < 2:
        raise ValueError('收敛曲线至少需 2 个含能量的点(其余尚未跑完?)')
    xs = [float(p['x']) for p in pts]
    e_ref = pts[-1]['energy']                          # 以最高参数点为参考零
    per = float(natoms) if natoms else 1.0
    ys = [(p['energy'] - e_ref) / per * 1000.0 for p in pts]   # meV(/atom)
    thr = float(threshold_mev)

    fig_w = width if width is not None else SINGLE_COL
    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.72)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        # 收敛判据阈值带:相对参考零 ±thr(收敛即落入带内)
        ax.axhspan(-thr, thr, color='#9ECAE1', alpha=0.30, zorder=0,
                   label=f'±{thr:g} meV{"/atom" if natoms else ""}')
        ax.axhline(0.0, color=ZERO_LINE_COLOR, lw=0.7, ls=(0, (5, 3)), zorder=1)
        ax.plot(xs, ys, color=colors[0], lw=1.4, marker='o', ms=5,
                mec='black', mew=0.5, zorder=3)
        if converged_at is not None:
            xc = float(converged_at)
            ax.axvline(xc, color='#DDAA33', lw=1.0, ls=(0, (4, 3)), zorder=2)
            yc = next((y for x, y in zip(xs, ys) if abs(x - xc) < 1e-9), None)
            if yc is not None:
                ax.plot([xc], [yc], marker='*', ms=13, color='#DDAA33',
                        mec='black', mew=0.6, ls='none', zorder=5)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel if ylabel is not None
                      else (r'$E - E_\mathrm{ref}$ (meV/atom)' if natoms
                            else r'$E - E_\mathrm{ref}$ (meV)'))
        if title:
            ax.set_title(title)
        ax.legend(loc='best')
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)
