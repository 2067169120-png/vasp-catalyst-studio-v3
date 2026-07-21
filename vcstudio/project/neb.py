"""NEB 结果解析与出图(Phase B 过渡态分析)——image 能量 → MEP 能垒 + 质量闸 + 出版级图。

三件套(纯函数为主,离线可测):
- ``parse_neb_energies``:读作业目录各 image(00..N+1)的 OSZICAR/OUTCAR → 绝对能量、
  相对初态能量、正/逆向能垒、过渡态 image 序号、爬坡收敛位、逐 image 力。
- ``neb_profile_plot``:出版级最小能量路径(MEP)曲线(复用 external.native_charts 的论文级
  风格层;matplotlib 延迟 import,无则 ImportError 上抛由调用方降级)。
- ``neb_quality_gate``:质量闸(未收敛/疑无垒/最高点在端点/单调无极值)→ 中文 issue 列表,
  供后续 campaign 三态语义对接(此处纯函数,不落状态)。

能量口径:各 image 取 OSZICAR 末行 E0(σ→0 外推,与报告口径一致);缺 OSZICAR 退 OUTCAR
的 energy(sigma->0)。逐 image 力取 OUTCAR 末离子步 |F|max(复用 cluster.convergence 纯解析)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

from vcstudio.cluster import convergence

# 逐 image 力收敛判据(eV/Å):无 EDIFFG 信息时的默认阈值(CI-NEB 常用 |F|max<0.05)。
FORCE_TOL = 0.05
# 疑"无垒"阈值(eV):正向能垒低于此值疑初末态过近/无过渡态。
BARRIER_MIN = 0.05

_SIGMA0_RE = re.compile(r'energy\(sigma->0\)\s*=\s*([-+0-9.Ee]+)')
_FRAME_RE = re.compile(r'^\d+$')


def _read_text(path) -> str | None:
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _frame_dirs(job_dir) -> list:
    """作业目录下的 image 子目录名(纯数字,升序):00,01,...,N+1。"""
    root = Path(job_dir)
    if not root.is_dir():
        return []
    names = [p.name for p in root.iterdir() if p.is_dir() and _FRAME_RE.match(p.name)]
    return sorted(names, key=lambda s: int(s))


def _frame_energy_evidence(frame_dir) -> tuple[float | None, str | None]:
    """单 image 能量与实际证据文件；不从 POSCAR/目录名推断。"""
    osz = _read_text(os.path.join(frame_dir, 'OSZICAR'))
    if osz:
        steps = convergence.parse_oszicar(osz)
        if steps and steps[-1].get('E0') is not None:
            return float(steps[-1]['E0']), 'OSZICAR:E0'
    out = _read_text(os.path.join(frame_dir, 'OUTCAR'))
    if out:
        hits = _SIGMA0_RE.findall(out)
        if hits:
            try:
                return float(hits[-1]), 'OUTCAR:energy(sigma->0)'
            except ValueError:
                return None, None
    return None, None


def _frame_energy(frame_dir) -> float | None:
    """向后兼容的单值入口。"""
    return _frame_energy_evidence(frame_dir)[0]


def _manifest_endpoint_energy(job_dir, frame: str, role: str):
    """仅接受 derive_neb 写入且带来源证据的端点能量。"""
    try:
        from vcstudio.shared import manifest as manifest_mod
        value = manifest_mod.load_manifest(job_dir) or {}
    except Exception:
        return None, None
    inputs = value.get('inputs') or {}
    endpoints = inputs.get('neb_endpoints') or {}
    if not isinstance(endpoints, dict):
        return None, None
    record = endpoints.get(role) or {}
    if not isinstance(record, dict) or record.get('target_frame') != frame \
            or record.get('trusted') is not True:
        return None, None
    energy = record.get('energy_e0_eV')
    if (isinstance(energy, bool) or not isinstance(energy, (int, float))
            or not math.isfinite(float(energy))):
        return None, None
    source = str(record.get('energy_source') or '').strip()
    if source.startswith('copied:'):
        evidence_file = source[len('copied:'):].split(':', 1)[0]
        files = record.get('files') or []
        has_hash = any(isinstance(item, dict)
                       and item.get('name') == evidence_file and item.get('sha256')
                       for item in files)
        if not has_hash:
            return None, None
    elif source.startswith('source_manifest:'):
        if record.get('source_state') != 'DONE' or not record.get('source_job_id'):
            return None, None
    else:
        return None, None
    return float(energy), source


def _frame_fmax(frame_dir) -> float | None:
    """单 image 末离子步 |F|max(eV/Å);缺 OUTCAR/无力块 → None。"""
    out = _read_text(os.path.join(frame_dir, 'OUTCAR'))
    if not out:
        return None
    f = convergence.parse_outcar_fmax(out)
    return f[-1] if f else None


def parse_neb_energies(job_dir) -> dict:
    """解析 NEB 作业各 image 能量 → MEP 能垒与过渡态。

    Returns:
        {
          'energies':  [E00, E01, ..., E(N+1)]   绝对能量 eV(缺帧为 None),
          'rel':       [相对初态 eV]              rel[i]=E[i]−E[0](缺帧 None),
          'barrier_f': 正向能垒 eV                = max(rel),
          'barrier_r': 逆向能垒 eV                = max(E−E_fin)(末态缺 → None),
          'ts_index':  过渡态 image 序号(rel 最高点,0 基帧号),
          'climbing_converged': bool             逐 image |F|max 全 < FORCE_TOL,
          'per_image_forces':   [|F|max, ...]     各帧末离子步力(缺 None),
          'n_frames':  帧数,'warnings': [中文告警],
        }

    帧数 < 3(至少 00/01/02)或初态能量缺失 → ValueError(无法定基线)。
    最高点落在端点 → warnings 提示能垒可能未被 image 采样覆盖。
    """
    frames = _frame_dirs(job_dir)
    if len(frames) < 3:
        raise ValueError(
            f'NEB 作业目录 image 子目录不足(找到 {len(frames)} 个,至少需 00/01/02);'
            f'请确认目录为标准 NEB 布局:{job_dir}')

    root = Path(job_dir)
    pairs = [_frame_energy_evidence(root / fr) for fr in frames]
    energies = [item[0] for item in pairs]
    energy_sources = [item[1] for item in pairs]
    forces = [_frame_fmax(root / fr) for fr in frames]

    # VASP NEB 只计算中间 image。端点能量来自生成时带入的已算证据；
    # 当回收时远端没有端点 OSZICAR/OUTCAR，才使用经哈希/源清单认证的值。
    fallback_notes = []
    for idx, role in ((0, 'start'), (len(frames) - 1, 'end')):
        if energies[idx] is not None:
            continue
        value, source = _manifest_endpoint_energy(root, frames[idx], role)
        if value is not None:
            energies[idx] = value
            energy_sources[idx] = source
            fallback_notes.append(
                f'{frames[idx]} 端点使用 job.yaml 中的可追溯能量({source})。')

    if energies[0] is None:
        raise ValueError(
            '无法读取初态(00)能量(缺 OSZICAR/OUTCAR 或解析失败);NEB 能垒需以初态为基线。')

    e0 = energies[0]
    rel = [(e - e0 if e is not None else None) for e in energies]
    valid = [(i, r) for i, r in enumerate(rel) if r is not None]
    ts_index, barrier_f = max(valid, key=lambda t: t[1])

    e_fin = energies[-1]
    barrier_r = None
    if e_fin is not None:
        barrier_r = max(e - e_fin for e in energies if e is not None)

    # 逐 image(中间帧 01..N)力收敛:全部可读且 < FORCE_TOL 方判爬坡收敛
    inter_forces = forces[1:-1]
    climbing_converged = bool(inter_forces) and all(
        f is not None and f < FORCE_TOL for f in inter_forces)

    warnings: list = list(fallback_notes)
    n_frames = len(frames)
    if ts_index == 0 or ts_index == n_frames - 1:
        warnings.append(
            '能垒最高点落在端点,能垒可能未被 image 采样覆盖,建议加密 image 或检查路径。')
    if e_fin is None:
        warnings.append('无法读取末态(N+1)能量,逆向能垒不可得。')
    if any(e is None for e in energies[1:-1]):
        warnings.append('部分中间 image 能量缺失,能垒按可读 image 估计(可能偏低)。')

    return {
        'energies': energies, 'rel': rel,
        'barrier_f': barrier_f, 'barrier_r': barrier_r,
        'ts_index': ts_index, 'climbing_converged': climbing_converged,
        'per_image_forces': forces, 'n_frames': n_frames, 'warnings': warnings,
        'energy_sources': energy_sources,
    }


def neb_profile_plot(data: dict, out_path, *, title: str = '',
                     labels=('IS', 'TS', 'FS'), point_labels: bool = False,
                     formats=('png', 'pdf')) -> list:
    """出版级最小能量路径(MEP)曲线:相对能量-反应坐标折线 + TS 红点 + 正/逆向 Ea 标注。

    复用 external.native_charts 的论文级风格层(apply_paper_style/_new_figure/_save_dual/
    chem_label,import 使用不改 native_charts)。data 取 parse_neb_energies 的返回。
    matplotlib 延迟 import(经 _new_figure),未装 → ImportError 上抛,由调用方降级。

    Args:
        labels: (初态, 过渡态, 末态) 三态标注文本;传 None/空则不标。
        point_labels: True 时每个 image 点内嵌小序号标签。
    Returns: 导出文件绝对路径列表(与 formats 同序)。
    """
    from vcstudio.external import native_charts as nc

    rel = list(data.get('rel') or [])
    xs = [i for i, r in enumerate(rel) if r is not None]
    if len(xs) < 2:
        raise ValueError('NEB 能量剖面至少需 2 个有效能量点')
    ys = [rel[i] for i in xs]
    ts_index = data.get('ts_index')
    barrier_f = data.get('barrier_f')
    barrier_r = data.get('barrier_r')

    with nc.apply_paper_style():
        fig, ax = nc._new_figure(width=nc.SINGLE_COL, aspect=0.72)
        # 零线(初态参考)
        ax.axhline(0.0, color=nc.ZERO_LINE_COLOR, lw=0.8, ls=(0, (5, 3)), zorder=1)
        # MEP 折线 + 点
        ax.plot(xs, ys, color='#4477AA', lw=1.6, marker='o', ms=5,
                mec='black', mew=0.5, zorder=3)

        # 过渡态红点 + Ea 标注(正/逆向)
        if ts_index is not None and rel[ts_index] is not None:
            tx, ty = ts_index, rel[ts_index]
            ax.plot([tx], [ty], marker='o', ms=8, color=nc.PDS_COLOR,
                    mec='black', mew=0.6, zorder=5)
            if barrier_f is not None:
                ax.annotate(rf'$E_\mathrm{{a}}$ = {barrier_f:.2f} eV', (tx, ty),
                            xytext=(0, 9), textcoords='offset points', ha='center',
                            va='bottom', color=nc.PDS_COLOR, fontsize=8)
            if barrier_r is not None:
                ax.annotate(rf'$E_\mathrm{{a}}^{{\mathrm{{rev}}}}$ = {barrier_r:.2f} eV',
                            (tx, ty), xytext=(0, -11), textcoords='offset points',
                            ha='center', va='top', color='#555555', fontsize=7)

        # IS/TS/FS 三态标注
        if labels:
            lab = list(labels) + [''] * (3 - len(labels))
            if lab[0]:
                ax.annotate(nc.chem_label(lab[0]), (xs[0], ys[0]), xytext=(4, -10),
                            textcoords='offset points', ha='left', va='top', fontsize=7.5)
            if lab[2]:
                ax.annotate(nc.chem_label(lab[2]), (xs[-1], ys[-1]), xytext=(-4, -10),
                            textcoords='offset points', ha='right', va='top', fontsize=7.5)
            if lab[1] and ts_index is not None and rel[ts_index] is not None:
                ax.annotate(nc.chem_label(lab[1]), (ts_index, rel[ts_index]),
                            xytext=(0, 22), textcoords='offset points', ha='center',
                            va='bottom', fontsize=7.5, color=nc.PDS_COLOR)

        if point_labels:
            for i in xs:
                ax.annotate(f'{i:02d}', (i, rel[i]), xytext=(3, 3),
                            textcoords='offset points', ha='left', va='bottom',
                            fontsize=6, color='#888888')

        ax.set_xlabel('Reaction coordinate (image)')
        ax.set_ylabel(r'$\Delta E$ (eV)')
        ax.set_xticks(xs)
        ax.margins(x=0.08, y=0.16)
        if title:
            ax.set_title(title)
        return nc._save_dual(fig, out_path, formats)


def neb_quality_gate(data: dict) -> dict:
    """NEB 结果质量闸 → ``{'ok', 'issues': [中文...]}``(纯函数,供 campaign validated 语义对接)。

    判据:
    - 爬坡镜像力未收敛 → 过渡态/能垒不可信;
    - 正向能垒 < BARRIER_MIN(疑无垒/初末态过近);
    - 最高点落在端点(未被 image 采样覆盖);
    - 相对能量沿反应坐标单调无极值(疑非过渡路径,未跨鞍点)。
    """
    issues: list = []

    if not data.get('climbing_converged'):
        issues.append('爬坡镜像(CI-NEB)力未收敛,过渡态/能垒尚不可信;请继续优化或检查收敛判据。')

    barrier_f = data.get('barrier_f')
    if barrier_f is not None and barrier_f < BARRIER_MIN:
        issues.append(
            f'正向能垒仅 {barrier_f:.3f} eV(< {BARRIER_MIN} eV),疑无势垒/初末态过近,请核对反应路径。')

    rel = [r for r in (data.get('rel') or []) if r is not None]
    n_frames = int(data.get('n_frames') or len(data.get('rel') or []))
    ts_index = data.get('ts_index')
    if ts_index is not None and n_frames and ts_index in (0, n_frames - 1):
        issues.append('能垒最高点落在端点,未被中间 image 采样覆盖;建议加密 image 或检查初末态设置。')

    if len(rel) >= 3:
        non_decreasing = all(rel[i + 1] >= rel[i] - 1e-9 for i in range(len(rel) - 1))
        non_increasing = all(rel[i + 1] <= rel[i] + 1e-9 for i in range(len(rel) - 1))
        if non_decreasing or non_increasing:
            issues.append('反应坐标上能量单调无极值,疑非过渡路径(未跨越鞍点)。')

    return {'ok': not issues, 'issues': issues}
