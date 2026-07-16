"""vasprun.xml DOS 流式解析(C4)——总 DOS 抓 <total>、投影抓 <partial>,读完即停。

vasprun.xml 可达几十 MB(投影/波函数段占大头):ElementTree.iterparse 流式,
parse_vasprun_dos 命中 `</dos>`、parse_vasprun_partial 命中 `</partial>` 立即
返回,绝不解析后面的 <projected> 大段。输入 file-like(测试 io.StringIO)。
坏 XML/截断/无对应数据段 → ValueError(api 层兜成结构化 error)。
d_band_center:投影 DOS 的一阶矩(可选二阶矩宽度),空/零投影 → None,绝不编数。
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET


def parse_vasprun_dos(fileobj) -> dict:
    """file-like → ``{'efermi','energies','spin_up','spin_down'|None}``。

    结构(VASP 5/6 一致):``<dos><i name="efermi">…</i><total><array>…
    <set><set comment="spin 1"><r>E total integrated</r>…`` 。
    """
    efermi = None
    spins: list[list[list[float]]] = []   # [spin][row] -> [energy, total, ...]
    cur: list[list[float]] | None = None
    in_total = False
    try:
        for event, el in ET.iterparse(fileobj, events=('start', 'end')):
            if event == 'start':
                if el.tag == 'total':
                    in_total = True
                elif in_total and el.tag == 'set' and \
                        (el.get('comment') or '').startswith('spin'):
                    cur = []
                    spins.append(cur)
                continue
            # end 事件
            if el.tag == 'i' and el.get('name') == 'efermi':
                try:
                    efermi = float((el.text or '').strip())
                except ValueError:
                    pass
            elif el.tag == 'r' and in_total and cur is not None:
                toks = (el.text or '').split()
                if len(toks) >= 2:
                    try:
                        cur.append([float(toks[0]), float(toks[1])])
                    except ValueError:
                        pass
            elif el.tag == 'total':
                in_total = False
            elif el.tag == 'dos':
                break          # DOS 段读完,立即停——不碰后面的投影大段
            el.clear()         # 流式:释放已处理节点内存
    except ET.ParseError as e:
        raise ValueError(f'vasprun.xml 解析失败(文件损坏或截断):{e}')

    if not spins or not spins[0]:
        raise ValueError('该 vasprun.xml 无 DOS 数据(需静态/DOS 计算产出)')
    if efermi is None:
        raise ValueError('vasprun.xml 缺 efermi,无法对齐费米能级')
    return {
        'efermi': efermi,
        'energies': [row[0] for row in spins[0]],
        'spin_up': [row[1] for row in spins[0]],
        'spin_down': [row[1] for row in spins[1]] if len(spins) > 1 else None,
    }


# ── 投影 DOS(<partial>,LORBIT≥10 产出)─────────────────────────────────────
# 列布局(列 0 = 能量):
# LORBIT=11(分 m):s | py pz px | dxy dyz dz2 dxz dx2(-y2) [| f×7] → ≥10 列
# LORBIT=10(不分 m):s | p | d [| f]                              → 4~5 列
_ORB_COLS_LM = {'s': (1,), 'p': (2, 3, 4), 'd': (5, 6, 7, 8, 9),
                'f': (10, 11, 12, 13, 14, 15, 16)}
_ORB_COLS_SPD = {'s': (1,), 'p': (2,), 'd': (3,), 'f': (4,)}
_ION_RE = re.compile(r'ion\s+(\d+)')


def _orbital_cols(orbitals: tuple, width: int) -> tuple:
    """轨道名 + 数据行列数 → 目标列号(按列数自动判 LORBIT=11/10 布局)。"""
    table = _ORB_COLS_LM if width >= 10 else _ORB_COLS_SPD
    cols = tuple(c for o in orbitals for c in table[o])
    if not cols:
        raise ValueError('orbitals 为空,无轨道可投影')
    if max(cols) >= width:
        raise ValueError(
            f'该 vasprun 投影行仅 {width} 列,不含轨道 {tuple(orbitals)} 的全部分量'
            '(f 轨道需赝势含 f 通道;LORBIT=10 只有 s/p/d 汇总列)')
    return cols


def parse_vasprun_partial(fileobj, ions=None, orbitals: tuple = ('d',)) -> dict:
    """流式解析 <partial> 投影 DOS → ``{'efermi','energies','dos'}``。

    ions:     要累加的原子序号集合,**1 起**(对齐 vasprun 的 ``comment="ion N"``
              与 POSCAR 原子顺序);None = 全部原子求和。
    orbitals: 轨道字母元组,取值 's'/'p'/'d'/'f'(默认 ('d',));同一轨道的
              全部 m 分量与两个自旋通道都求和(d 带中心的常规口径)。
    列布局按数据行宽度自动判定:≥10 列按 LORBIT=11 分 m 布局
    (s / py pz px / dxy dyz dz2 dxz dx2),4~5 列按 LORBIT=10 汇总布局。
    命中 </partial> 立即 break(流式省内存,不碰后面的 <projected> 大段)。
    无 <partial> 段(LORBIT<10 或非 DOS 计算)→ ValueError 明示"无投影数据";
    ions 过滤后无匹配 → ValueError 点名可用范围。绝不编数。
    """
    orbitals = tuple(orbitals)
    bad = [o for o in orbitals if o not in _ORB_COLS_LM]
    if bad:
        raise ValueError(f'未知轨道 {bad}(可选 s/p/d/f)')
    want = None if ions is None else {int(i) for i in ions}
    efermi = None
    in_partial = False
    saw_partial = False
    ion_taken = False        # 当前 <set comment="ion N"> 是否在过滤集合内
    collecting = False       # 当前处于目标 ion 的某个 spin <set> 内
    n_ions_seen = 0
    n_blocks_done = 0        # 已累加完的 (ion, spin) 块数
    row_idx = 0
    cols: tuple | None = None
    energies: list[float] = []
    dos: list[float] = []
    try:
        for event, el in ET.iterparse(fileobj, events=('start', 'end')):
            if event == 'start':
                if el.tag == 'partial':
                    in_partial = saw_partial = True
                elif in_partial and el.tag == 'set':
                    comment = el.get('comment') or ''
                    m = _ION_RE.match(comment.strip())
                    if m:
                        n_ions_seen += 1
                        ion_taken = want is None or int(m.group(1)) in want
                    elif comment.startswith('spin') and ion_taken:
                        collecting = True
                        row_idx = 0
                continue
            # end 事件
            if el.tag == 'i' and el.get('name') == 'efermi':
                try:
                    efermi = float((el.text or '').strip())
                except ValueError:
                    pass
            elif el.tag == 'r' and collecting:
                toks = (el.text or '').split()
                if cols is None:
                    cols = _orbital_cols(orbitals, len(toks))
                try:
                    e = float(toks[0])
                    v = sum(float(toks[c]) for c in cols)
                except (ValueError, IndexError):
                    raise ValueError(f'<partial> 数据行格式异常:{(el.text or "").strip()!r}')
                if n_blocks_done == 0:
                    energies.append(e)
                    dos.append(v)
                elif row_idx < len(energies):
                    dos[row_idx] += v      # 各 ion/spin 共享同一能量网格,逐行累加
                else:
                    raise ValueError('<partial> 各 ion/spin 能量网格长度不一致,文件异常')
                row_idx += 1
            elif el.tag == 'set' and collecting:
                # 最内层(spin)set 结束:一个 (ion, spin) 块累加完毕
                collecting = False
                if row_idx != len(energies):
                    raise ValueError('<partial> 各 ion/spin 能量网格长度不一致,文件异常')
                n_blocks_done += 1
            elif el.tag == 'partial':
                break          # 投影段读完,立即停——不碰后面的 <projected> 大段
            el.clear()         # 流式:释放已处理节点内存
    except ET.ParseError as e:
        raise ValueError(f'vasprun.xml 解析失败(文件损坏或截断):{e}')

    if not saw_partial:
        raise ValueError('该 vasprun.xml 无投影 DOS 数据(<partial> 段缺失):'
                         '需 LORBIT=11(或 ≥10)的静态/DOS 计算产出')
    if not energies:
        if want is not None and n_ions_seen:
            raise ValueError(f'ions 过滤后无匹配原子(该 vasprun 含 ion 1..{n_ions_seen},'
                             f'请求 {sorted(want)};序号 1 起,对齐 POSCAR 顺序)')
        raise ValueError('<partial> 段存在但未解析到任何投影数据(文件可能截断)')
    return {'efermi': efermi, 'energies': energies, 'dos': dos}


def d_band_center(energies, dos_d, efermi: float, *, return_width: bool = False):
    """d 带中心 ε_d = Σ(E−E_F)·ρ(E) / Σρ(E)(一阶矩,eV,相对费米能级)。

    return_width=True → 返回 (ε_d, w_d),w_d = sqrt(Σ(E−E_F)²ρ/Σρ − ε_d²)
    (二阶矩带宽)。约定:直接对传入网格求离散矩——若只要占据态口径,调用方
    自行把数组截到 E ≤ E_F 再传入。
    空数组或 Σρ ≤ 0(该原子/窗口无投影)→ 返回 None(绝不编数);
    energies 与 dos_d 长度不一致 → ValueError(调用方 bug,显式暴露)。
    """
    es, ds = list(energies), list(dos_d)
    if len(es) != len(ds):
        raise ValueError(f'energies({len(es)})与 dos_d({len(ds)})长度不一致')
    if not es:
        return None
    total = sum(ds)
    if total <= 0.0:
        return None
    m1 = sum((e - efermi) * d for e, d in zip(es, ds)) / total
    if not return_width:
        return m1
    m2 = sum((e - efermi) ** 2 * d for e, d in zip(es, ds)) / total
    return m1, math.sqrt(max(m2 - m1 * m1, 0.0))
