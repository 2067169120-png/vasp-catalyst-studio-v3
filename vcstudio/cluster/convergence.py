"""收敛过程解析(C1)——OSZICAR/OUTCAR → 逐离子步 E0/ΔE/|F|max 序列。

纯函数,零 IO,离线可测。口径与 diagnose.py 一致但不 import 其私有名:
- 离子步以 OSZICAR 的 ``F= ... E0=`` 行收尾;其上是若干 SCF 电子步行。
- ``E0`` 取 σ→0 外推能(与报告能量口径一致);离子步间 ``dE`` = |E0[i]-E0[i-1]|。
- ``|F|max`` 取 OUTCAR 每个 ``TOTAL-FORCE (eV/Angst)`` 块内各原子力模的最大值。
"""
from __future__ import annotations

import math
import re

# SCF 电子步行:VASP 各种 ALGO 前缀 + 迭代序号(与 diagnose._SCF_LINE_RE 同族)。
_SCF_LINE_RE = re.compile(
    r'^\s*(?:DAV|RMM|EDDAV|CG|DMP|QDMP|DIA|NONE):\s*(\d+)\s')
# F= 收尾行:行首离子步号 + E0= 值。
_STEP_NO_RE = re.compile(r'^\s*(\d+)\s+F=')
_E0_RE = re.compile(r'E0=\s*([-+]?[.\d]+(?:[eE][-+]?\d+)?)')


def parse_oszicar(text: str) -> list[dict]:
    """OSZICAR 文本 → 逐离子步 ``[{step,E0,dE,scf_iters}, ...]``。

    坏行(F= 行但 E0 解析失败)整步跳过;dE 为相对上一有效步的 |ΔE0|,首步 None。
    """
    if not text:
        return []
    steps: list[dict] = []
    scf_iters = 0
    prev_e0: float | None = None
    for line in text.splitlines():
        m_scf = _SCF_LINE_RE.match(line)
        if m_scf:
            scf_iters = int(m_scf.group(1))
            continue
        if ' F=' in line and 'E0=' in line:
            m_e0 = _E0_RE.search(line)
            if not m_e0:
                scf_iters = 0
                continue
            try:
                e0 = float(m_e0.group(1))
            except ValueError:
                scf_iters = 0
                continue
            m_no = _STEP_NO_RE.match(line)
            step_no = int(m_no.group(1)) if m_no else len(steps) + 1
            de = None if prev_e0 is None else abs(e0 - prev_e0)
            steps.append({'step': step_no, 'E0': e0, 'dE': de,
                          'scf_iters': scf_iters})
            prev_e0 = e0
            scf_iters = 0
    return steps


def parse_outcar_fmax(text: str) -> list[float]:
    """OUTCAR 文本 → 逐离子步 |F|max(eV/Å)。逐行流式,只在力块内累加。"""
    if not text:
        return []
    fmax: list[float] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if 'TOTAL-FORCE' not in lines[i]:
            i += 1
            continue
        i += 1
        # 跳过力块起始的分隔虚线行。
        if i < n and lines[i].strip() and set(lines[i].strip()) <= set('-'):
            i += 1
        cur_max = 0.0
        found = False
        while i < n:
            row = lines[i].split()
            if len(row) != 6:
                break
            try:
                fx, fy, fz = float(row[3]), float(row[4]), float(row[5])
            except ValueError:
                break
            mag = math.sqrt(fx * fx + fy * fy + fz * fz)
            if mag > cur_max:
                cur_max = mag
            found = True
            i += 1
        if found:
            fmax.append(cur_max)
    return fmax


def convergence_series(oszicar_text: str,
                       outcar_text: str | None = None) -> dict:
    """对齐 OSZICAR/OUTCAR → 前端可直接消费的序列 dict。

    返回键:``steps,E0,dE,fmax,scf_iters,have_forces,notes``。
    空/垃圾输入 → 空序列 + notes 说明,绝不抛。
    """
    notes: list[str] = []
    ionic = parse_oszicar(oszicar_text or '')
    fmax = parse_outcar_fmax(outcar_text or '') if outcar_text else []
    have_forces = bool(fmax)

    if not ionic:
        notes.append('未从 OSZICAR 解析到任何离子步(文件为空或格式异常)')
        return {'steps': [], 'E0': [], 'dE': [], 'fmax': [],
                'scf_iters': [], 'have_forces': False, 'notes': notes}

    n = len(ionic)
    if have_forces and len(fmax) != n:
        notes.append(
            f'OSZICAR 离子步 {n} 与 OUTCAR 力块 {len(fmax)} 数不一致,按较短对齐显示')
    if outcar_text and not have_forces:
        notes.append('OUTCAR 存在但未解析到力块(可能尚未完成首个离子步)')
    if not outcar_text:
        notes.append('无 OUTCAR,无法显示 |F|max')

    return {
        'steps': [s['step'] for s in ionic],
        'E0': [s['E0'] for s in ionic],
        'dE': [s['dE'] for s in ionic],
        'fmax': [fmax[i] if i < len(fmax) else None for i in range(n)],
        'scf_iters': [s['scf_iters'] for s in ionic],
        'have_forces': have_forces,
        'notes': notes,
    }
