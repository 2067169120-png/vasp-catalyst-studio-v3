"""Bader 电荷解析(纯函数零依赖):只解析 Henkelman bader 程序的 ACF.dat 产物。

本工具**不跑** bader 可执行(守确定性核心):请在服务器/本机自行运行
Henkelman 组的 bader 程序(全电子口径:先 CHGCAR_sum = AECCAR0+AECCAR2,
再 `bader CHGCAR -ref CHGCAR_sum`)生成 ACF.dat,拉回后用本模块解析。

口径(报告须显式写明):
- ACF.dat 每原子一行:``序号 X Y Z CHARGE MIN_DIST ATOMIC_VOL``;CHARGE 是
  Bader 分区内的**电子数**(不是净电荷)。
- ΔQ = ZVAL − CHARGE(ZVAL = POTCAR 该物种价电子数):
  ΔQ > 0 失电子(带正电/被氧化),ΔQ < 0 得电子(带负电/被还原)。
- ZVAL 从 POTCAR 头部 ``POMASS = …; ZVAL = …`` 行读取(每物种一行,按拼接
  顺序);vcstudio.generate.potcar 只管 ENMAX/TITEL,ZVAL 解析放本模块。
缺文件给中文指引,格式异常显式报错,绝不编数。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re

_ZVAL_RE = re.compile(r'ZVAL\s*=\s*([0-9.]+)')

_ACF_GUIDE = ('未找到 ACF.dat:{p}。本工具不代跑 Bader 分析,请先运行 Henkelman '
              'bader 程序(全电子口径:CHGCAR_sum = AECCAR0+AECCAR2,再 '
              '`bader CHGCAR -ref CHGCAR_sum`)生成 ACF.dat,再回来解析。')
_POTCAR_GUIDE = ('未找到 POTCAR:{p}。ΔQ = ZVAL − CHARGE 需要 POTCAR 的各物种 '
                 'ZVAL;请把作业目录的 POTCAR(与跑 Bader 的计算同一份)一并拉回。')


def _read_maybe_path(src, guide: str) -> str:
    """路径或文本 → 文本。str 含换行视为文本内容,否则视为路径(缺文件给中文指引)。"""
    if isinstance(src, os.PathLike):
        path = os.fspath(src)
    elif isinstance(src, str) and '\n' not in src:
        path = src
    else:
        return str(src)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        raise FileNotFoundError(guide.format(p=path)) from None


def parse_acf(path_or_text) -> list:
    """ACF.dat(路径或文本)→ 每原子 Bader 电子数列表(按原子序,与 POSCAR 对齐)。

    跳过表头(# 行)/分隔线/尾部汇总(VACUUM CHARGE 等);数据行取第 5 列
    CHARGE。原子序号必须从 1 连续递增(截断/拼接错的文件显式报错,不给半截数)。
    """
    text = _read_maybe_path(path_or_text, _ACF_GUIDE)
    charges: list[float] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith('#') or set(s) <= {'-'}:
            continue
        toks = s.split()
        try:
            idx = int(toks[0])
        except ValueError:
            continue                       # 表头/汇总等非数据行
        if idx != len(charges) + 1:
            raise ValueError(f'ACF.dat 原子序号不连续(期望 {len(charges) + 1},'
                             f'读到 {idx}):文件可能截断或拼接错误')
        if len(toks) < 5:
            raise ValueError(f'ACF.dat 第 {idx} 号原子行仅 {len(toks)} 列(需 ≥5:'
                             f'序号 X Y Z CHARGE …):{s!r}')
        try:
            charges.append(float(toks[4]))
        except ValueError:
            raise ValueError(f'ACF.dat 第 {idx} 号原子 CHARGE 列不是数值:{toks[4]!r}')
    if not charges:
        raise ValueError('ACF.dat 未解析到任何原子行(每原子一行:序号 X Y Z CHARGE …);'
                         '请确认是 Henkelman bader 程序的原始产物且未被截断')
    return charges


def read_potcar_zvals(path_or_text) -> list:
    """POTCAR(路径或文本)→ 各物种 ZVAL 列表(按拼接顺序,一物种一个值)。

    读每个物种块的 ``POMASS = …; ZVAL = …`` 行。多物种 POTCAR 是原文件顺序
    拼接,与 POSCAR 物种顺序一致(工具生成时已保证)。无 ZVAL 行 → ValueError。
    """
    text = _read_maybe_path(path_or_text, _POTCAR_GUIDE)
    zvals = [float(m) for m in _ZVAL_RE.findall(text)]
    if not zvals:
        raise ValueError('POTCAR 中未找到 ZVAL 行(POMASS = …; ZVAL = …):'
                         '请确认传入的是 VASP POTCAR 文件')
    return zvals


def expand_zvals(zvals: list, counts: list) -> list:
    """物种 ZVAL + POSCAR 各物种原子数 → 逐原子 ZVAL 列表(长度 Σcounts)。

    与 parse_acf 的原子序对齐(两者都按 POSCAR 顺序)。物种数不匹配 → ValueError。
    """
    if len(zvals) != len(counts):
        raise ValueError(f'物种数不匹配:POTCAR ZVAL {len(zvals)} 个 vs '
                         f'POSCAR counts {len(counts)} 个')
    out: list[float] = []
    for z, n in zip(zvals, counts):
        out.extend([float(z)] * int(n))
    return out


def charge_transfer(charges: list, zvals: list) -> list:
    """逐原子 ΔQ = ZVAL − CHARGE(单位 e)。

    charges 来自 parse_acf(Bader 电子数),zvals 是**逐原子** ZVAL(物种 ZVAL
    请先用 expand_zvals 按 POSCAR counts 展开)。ΔQ > 0 失电子(带正电)。
    长度不一致 → ValueError(绝不静默错位配对)。
    """
    if len(charges) != len(zvals):
        raise ValueError(f'原子数不匹配:ACF 电荷 {len(charges)} 个 vs 逐原子 ZVAL '
                         f'{len(zvals)} 个(物种 ZVAL 需先按 POSCAR counts 展开,'
                         '见 expand_zvals)')
    return [z - c for z, c in zip(zvals, charges)]
