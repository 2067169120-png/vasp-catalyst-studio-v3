"""vasprun.xml DOS 流式解析(C4)——总 DOS 抓 <total>、投影抓 <partial>,读完即停。

vasprun.xml 可达几十 MB(投影/波函数段占大头):ElementTree.iterparse 流式,
parse_vasprun_dos 命中 `</dos>`、parse_vasprun_partial 命中 `</partial>` 立即
返回,绝不解析后面的 <projected> 大段。输入 file-like(测试 io.StringIO)。
坏 XML/截断/无对应数据段 → ValueError(api 层兜成结构化 error)。
d_band_center:投影 DOS 的一阶矩(可选二阶矩宽度),空/零投影 → None,绝不编数。
"""
from __future__ import annotations

import io
import math
import os
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


# ══ F19:完整投影 DOS(每原子×每轨道×每自旋)+ d 带中心(梯形积分口径)══════════
# 与上面 parse_vasprun_partial(聚合式,直接给某原子集/轨道族的一条曲线)互补:
# parse_vasprun_pdos 保留**全分辨**(逐原子、逐轨道族 s/p/d[/f]、逐自旋),供
# PDOS 出版图(自旋镜像 + d-p 杂化叠加)与 d 带中心/自旋分辨中心/杂化重叠积分。

def _as_vasprun_fileobj(src):
    """path / XML 文本 / file-like → ``(fileobj, should_close)``。

    - 有 ``.read`` → 直接当 file-like(测试 io.StringIO),不代关。
    - PathLike / 不含换行且非 '<' 起头的 str → 当文件路径打开。
    - 含换行或 '<' 起头的 str → 当 XML 文本内容,包成 StringIO。
    """
    if hasattr(src, 'read'):
        return src, False
    if isinstance(src, os.PathLike):
        return open(os.fspath(src), 'r', encoding='utf-8', errors='replace'), True
    if isinstance(src, str):
        if '\n' in src or src.lstrip().startswith('<'):
            return io.StringIO(src), True
        return open(src, 'r', encoding='utf-8', errors='replace'), True
    raise TypeError('vasprun 输入须为文件路径 / XML 文本 / file-like')


def _available_orbitals(width: int):
    """据 <partial> 数据行列数返回 (可用轨道族列表, 列布局表)。

    ≥10 列 → LORBIT=11 分 m 布局(s/p/d[/f] 按 m 分量求和);4~5 列 → LORBIT=10
    汇总布局。只收布局里列号不越界的轨道族(如 4 列布局无 f)。
    """
    table = _ORB_COLS_LM if width >= 10 else _ORB_COLS_SPD
    avail = [o for o in ('s', 'p', 'd', 'f') if max(table[o]) < width]
    return avail, table


def parse_vasprun_pdos(path_or_text) -> dict:
    """流式解析 vasprun.xml 的 <partial> → 全分辨投影 DOS(不聚合)。

    返回::

        {'energies': [...], 'efermi': float, 'spin_polarized': bool,
         'ions': [{'index': 1, 'orbitals': {'s': {'up': [...], 'down': [...]},
                                            'p': {...}, 'd': {...}}}, ...]}

    - ``index`` 1 起,对齐 vasprun ``comment="ion N"`` 与 POSCAR 原子顺序。
    - 每个轨道族(s/p/d[/f])的 up/down 是**同一能量网格上**的该族全 m 分量之和;
      非自旋极化只有 'up' 键(spin_polarized=False)。
    - 内存:ElementTree.iterparse 流式,命中 </partial> 立即 break,绝不解析后面的
      <projected> 大段(vasprun 可达百 MB)。坏/截断 XML → ValueError。
    - 无 <partial>(LORBIT<10 或非 DOS 计算)→ ValueError 明示"无投影数据"。
    """
    fileobj, should_close = _as_vasprun_fileobj(path_or_text)
    efermi = None
    in_partial = saw_partial = False
    collecting = False
    cur_ion_index = None
    ion_orbitals = None
    spin_key = None
    cols_table = None
    avail = None
    energies: list[float] = []
    ions: list[dict] = []
    spin_polarized = False
    try:
        for event, el in ET.iterparse(fileobj, events=('start', 'end')):
            if event == 'start':
                if el.tag == 'partial':
                    in_partial = saw_partial = True
                elif in_partial and el.tag == 'set':
                    comment = (el.get('comment') or '').strip()
                    m = _ION_RE.match(comment)
                    if m:
                        cur_ion_index = int(m.group(1))
                        ion_orbitals = {}
                        ions.append({'index': cur_ion_index, 'orbitals': ion_orbitals})
                    elif comment.startswith('spin') and cur_ion_index is not None:
                        try:
                            sp = int(comment.split()[-1])
                        except ValueError:
                            sp = 1
                        spin_key = 'up' if sp == 1 else 'down'
                        if sp >= 2:
                            spin_polarized = True
                        collecting = True
                continue
            # end 事件
            if el.tag == 'i' and el.get('name') == 'efermi':
                try:
                    efermi = float((el.text or '').strip())
                except ValueError:
                    pass
            elif el.tag == 'r' and collecting:
                toks = (el.text or '').split()
                if cols_table is None:
                    avail, cols_table = _available_orbitals(len(toks))
                try:
                    e = float(toks[0])
                    row = {o: sum(float(toks[c]) for c in cols_table[o]) for o in avail}
                except (ValueError, IndexError):
                    raise ValueError(
                        f'<partial> 数据行格式异常:{(el.text or "").strip()!r}')
                if len(ions) == 1 and spin_key == 'up':
                    energies.append(e)              # 第一个 (ion1,spin1) 块定能量网格
                for o in avail:
                    ion_orbitals.setdefault(o, {}).setdefault(spin_key, []).append(row[o])
            elif el.tag == 'set' and collecting:
                collecting = False
                spin_key = None
            elif el.tag == 'partial':
                break              # 投影段读完立即停,不碰后面的 <projected> 大段
            el.clear()
    except ET.ParseError as e:
        raise ValueError(f'vasprun.xml 解析失败(文件损坏或截断):{e}')
    finally:
        if should_close:
            fileobj.close()

    if not saw_partial:
        raise ValueError('该 vasprun.xml 无投影 DOS 数据(<partial> 段缺失):'
                         '需 LORBIT=11(或 ≥10)的静态/DOS 计算产出')
    if not ions or not energies:
        raise ValueError('<partial> 段存在但未解析到任何投影数据(文件可能截断)')
    return {'energies': energies, 'efermi': efermi,
            'ions': ions, 'spin_polarized': spin_polarized}


def sum_pdos(pdos: dict, *, ions=None, orbitals=('d',), spin: str = 'both') -> dict:
    """按原子集合 + 轨道族聚合 parse_vasprun_pdos 的输出 → 一条(或两条自旋)曲线。

    Args:
        pdos: parse_vasprun_pdos 的返回。
        ions: 要累加的原子序号集合(**1 起**,对齐 POSCAR);None = 全部原子。
        orbitals: 轨道族元组,取值 's'/'p'/'d'/'f'(默认 ('d',));同族全 m 分量求和。
        spin: 'up' / 'down' / 'both'。'both' 时 dos_up、dos_down 都给(非自旋极化
              dos_down=None);'up' 只给上自旋,'down' 只给下自旋。

    Returns:
        ``{'energies', 'dos_up', 'dos_down'}``(未取的通道置 None)。
        ions 过滤无匹配 / 请求轨道不在投影里 / spin 非法 → ValueError。
    """
    if spin not in ('up', 'down', 'both'):
        raise ValueError(f"spin 只能是 up/down/both,收到 {spin!r}")
    orbitals = tuple(orbitals)
    energies = list(pdos['energies'])
    n = len(energies)
    want = None if ions is None else {int(i) for i in ions}
    sel = [ion for ion in pdos['ions'] if want is None or ion['index'] in want]
    if not sel:
        avail = [ion['index'] for ion in pdos['ions']]
        raise ValueError(f'ions 过滤后无匹配原子(该 vasprun 含 ion {avail};'
                         f'请求 {sorted(want)};序号 1 起,对齐 POSCAR 顺序)')
    all_orbs = set()
    for ion in pdos['ions']:
        all_orbs |= set(ion['orbitals'].keys())
    bad = [o for o in orbitals if o not in all_orbs]
    if bad:
        raise ValueError(f'请求轨道 {bad} 不在投影数据里(可用 {sorted(all_orbs)};'
                         'LORBIT=10 汇总布局无分 m,f 轨道需赝势含 f 通道)')
    up = [0.0] * n
    down = [0.0] * n if pdos.get('spin_polarized') else None
    for ion in sel:
        for o in orbitals:
            od = ion['orbitals'].get(o)
            if not od:
                continue
            for i, v in enumerate(od.get('up', [])):
                up[i] += v
            if down is not None:
                for i, v in enumerate(od.get('down', [])):
                    down[i] += v
    return {'energies': energies,
            'dos_up': up if spin in ('up', 'both') else None,
            'dos_down': down if spin in ('down', 'both') else None}


def _trapz(y, x) -> float:
    """梯形积分 ∫y dx(x 单调等长)。点数 < 2 → 0.0。"""
    s = 0.0
    for i in range(len(x) - 1):
        s += 0.5 * (y[i] + y[i + 1]) * (x[i + 1] - x[i])
    return s


def band_center(energies, dos, *, efermi: float, window=(-10.0, 2.0),
                occupied_only: bool = False) -> dict:
    """d 带中心 ε_d = ∫E'ρ dE' / ∫ρ dE'(梯形积分;E' = E − E_F,相对费米能级)。

    宪法·口径可见:**积分窗口既是显式入参、也随结果返回**。窗口 (lo, hi) 以
    **相对费米能级** eV 计——只取网格点满足 lo ≤ (E − E_F) ≤ hi 者(不在边界插值);
    occupied_only=True 再截到 E ≤ E_F(占据态口径)。

    Returns:
        ``{'center_eV', 'window', 'n_states', 'occupied_only'}``。center_eV 相对 E_F;
        n_states = ∫ρ dE'(窗口内积分态数)。空窗口 / Σρ≤0(无投影)→ center_eV=None
        (绝不编数)。energies 与 dos 长度不一致 → ValueError。
    """
    es, ds = list(energies), list(dos)
    if len(es) != len(ds):
        raise ValueError(f'energies({len(es)})与 dos({len(ds)})长度不一致')
    lo, hi = float(window[0]), float(window[1])
    e_rel, rho = [], []
    for e, d in zip(es, ds):
        er = e - efermi
        if er < lo or er > hi:
            continue
        if occupied_only and er > 0.0:
            continue
        e_rel.append(er)
        rho.append(d)
    n_states = _trapz(rho, e_rel) if len(e_rel) >= 2 else 0.0
    center = None
    if len(e_rel) >= 2 and n_states > 0.0:
        center = _trapz([er * r for er, r in zip(e_rel, rho)], e_rel) / n_states
    return {'center_eV': center, 'window': (lo, hi),
            'n_states': n_states, 'occupied_only': bool(occupied_only)}


def spin_band_centers(energies, dos_up, dos_down, *, efermi: float,
                      window=(-10.0, 2.0), occupied_only: bool = False) -> dict:
    """自旋分辨 d 带中心:分别对上/下自旋 DOS 求 band_center(口径同上)。

    Returns:
        ``{'up': <band_center dict>, 'down': <band_center dict|None>, 'window'}``。
        dos_down 为 None(非自旋极化)→ 'down' 为 None。
    """
    up = band_center(energies, dos_up, efermi=efermi, window=window,
                     occupied_only=occupied_only)
    down = None
    if dos_down is not None:
        down = band_center(energies, dos_down, efermi=efermi, window=window,
                           occupied_only=occupied_only)
    return {'up': up, 'down': down, 'window': (float(window[0]), float(window[1]))}


def dp_overlap(energies, dos_d, dos_p) -> float:
    """d-p 杂化重叠积分 S = ∫ min(ρ_d, ρ_p) dE(梯形积分,单位 states)。

    衡量金属 d 与吸附质 p 投影 DOS 在能量轴上的重叠面积——重叠越大,d-p 杂化
    (成键)越强。**归一化说明**:返回的是**原始重叠积分**(量纲 states,依赖两条
    曲线各自的投影归一);要得到无量纲杂化指数,调用方按需除以 min(∫ρ_d, ∫ρ_p) 或
    几何平均 sqrt(∫ρ_d·∫ρ_p)。三数组须等长(同能量网格),否则 ValueError。
    """
    es, dd, dp = list(energies), list(dos_d), list(dos_p)
    if not (len(es) == len(dd) == len(dp)):
        raise ValueError(f'energies({len(es)})/dos_d({len(dd)})/dos_p({len(dp)})'
                         '长度不一致(须同一能量网格)')
    return _trapz([min(a, b) for a, b in zip(dd, dp)], es)
