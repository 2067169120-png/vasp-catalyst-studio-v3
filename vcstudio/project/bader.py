"""Bader 电荷链(F20):AECCAR 合并 → 探测/调用 bader → 解析 ACF.dat → ΔQ 守恒。

纯函数核心(parse_acf/read_potcar_zvals/expand_zvals/charge_transfer)零依赖、离线可测;
run_bader 走"探测→调用→降级"(对齐 external/povray_render.py 口径):bader 可执行
不存在/超时/非零退出 → 返回结构化 error(带中文指引与已备好的 CHGCAR_sum),绝不崩。

全电子口径(报告须显式写明):先 CHGCAR_sum = AECCAR0(核)+ AECCAR2(价),
再 `bader CHGCAR -ref CHGCAR_sum` 生成 ACF.dat。AECCAR0/AECCAR2 来自 LAECHG=.TRUE.
的静态作业(见 generate/estatic.py purpose='bader')。

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


def parse_acf(path_or_text, potcar_zvals=None):
    """ACF.dat(路径或文本)→ Bader 电子数(单参)或逐原子电荷字典(带 ZVAL)。

    - ``parse_acf(acf)``:返回每原子 Bader 电子数**列表**(第 5 列 CHARGE,按原子序,
      与 POSCAR 对齐)——向后兼容的既有口径。
    - ``parse_acf(acf, potcar_zvals)``:``potcar_zvals`` 为**逐原子** ZVAL(物种 ZVAL
      请先用 expand_zvals 按 POSCAR counts 展开),返回::

          {'atoms': [{'index': 1, 'charge': 8.14, 'delta_q': -0.14}, ...],
           'charges': [...], 'delta_q': [...], 'sum_delta_q': float, 'warnings': [...]}

      ΔQ = ZVAL − CHARGE(>0 失电子/被氧化);守恒校验 ∑ΔQ≈0,|∑ΔQ|>0.05 e → warning。

    原子序号必须从 1 连续递增(截断/拼接错的文件显式报错,不给半截数)。
    potcar_zvals 与原子数不等 → ValueError(点名 expand_zvals)。
    """
    charges = _parse_acf_charges(_read_maybe_path(path_or_text, _ACF_GUIDE))
    if potcar_zvals is None:
        return charges
    zvals = [float(z) for z in potcar_zvals]
    if len(zvals) != len(charges):
        raise ValueError(f'原子数不匹配:ACF 电荷 {len(charges)} 个 vs 传入 ZVAL '
                         f'{len(zvals)} 个(物种 ZVAL 需先按 POSCAR counts 展开,'
                         '见 expand_zvals)')
    dq = [z - c for z, c in zip(zvals, charges)]
    atoms = [{'index': i + 1, 'charge': charges[i], 'delta_q': dq[i]}
             for i in range(len(charges))]
    total = sum(dq)
    warnings: list[str] = []
    if abs(total) > 0.05:
        warnings.append(f'Bader 电荷不守恒:∑ΔQ = {total:+.4f} e(|>0.05 e|);'
                        '请检查参考电荷是否用了 AECCAR0+AECCAR2、网格(NG*F)是否够密')
    return {'atoms': atoms, 'charges': charges, 'delta_q': dq,
            'sum_delta_q': total, 'warnings': warnings}


def _parse_acf_charges(text: str) -> list:
    """ACF.dat 文本 → 每原子 Bader 电子数列表(数据行第 5 列 CHARGE)。"""
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


def group_transfer(acf, adsorbate_indices) -> dict:
    """吸附质净得失电子:对吸附质原子集合的 ΔQ 求和。

    Args:
        acf: parse_acf(acf_text, potcar_zvals) 的返回字典(取其 'delta_q'),
             或直接是逐原子 ΔQ 列表。
        adsorbate_indices: 吸附质原子序号集合(**1 起**,对齐 POSCAR / ACF 原子序)。

    Returns:
        ``{'indices', 'n_atoms', 'net_delta_q', 'electrons_gained'}``。
        net_delta_q = Σ吸附质 ΔQ(>0 吸附质失电子);electrons_gained = −net_delta_q
        (>0 表示吸附质**净得**电子,即被还原)。序号越界/为空 → ValueError。
    """
    if isinstance(acf, dict):
        dq = acf.get('delta_q')
        if dq is None:
            raise ValueError("acf 缺 'delta_q':请用 parse_acf(acf_text, potcar_zvals) 生成")
    else:
        dq = list(acf)
    n = len(dq)
    idxs = sorted({int(i) for i in adsorbate_indices})
    if not idxs:
        raise ValueError('adsorbate_indices 为空,无法统计吸附质电荷转移')
    bad = [i for i in idxs if i < 1 or i > n]
    if bad:
        raise ValueError(f'吸附质序号越界 {bad}(有效 1..{n};序号 1 起,对齐 POSCAR)')
    net = sum(dq[i - 1] for i in idxs)
    return {'indices': idxs, 'n_atoms': len(idxs),
            'net_delta_q': net, 'electrons_gained': -net}


# ── Bader 可执行:探测 → 合参考 → 调用 → 降级(不崩)──────────────────────────

def find_bader_exe(configured: str = '') -> str | None:
    """探测 Henkelman 组 bader 可执行:配置路径 → PATH。找不到 → None(降级)。"""
    import shutil
    if configured and os.path.isfile(configured):
        return configured
    for name in ('bader', 'bader.exe', 'Bader'):
        p = shutil.which(name)
        if p:
            return p
    return None


def sum_aeccar(aeccar0_path, aeccar2_path, out_path) -> dict:
    """CHGCAR_sum = AECCAR0 + AECCAR2(纯 python 网格代数)→ 写 out_path。

    读两文件的头 + 网格,校验晶格/网格一致(不一致 → 中文 ValueError),逐点相加。
    AECCAR0(核电荷)+ AECCAR2(价电荷)= 全电子近似电荷,作 `bader -ref` 的参考。
    返回 ``{'out', 'n_grid', 'ngx', 'ngy', 'ngz'}``。
    """
    from vcstudio.project import chgdiff          # 延迟导入:CHGCAR 网格 I/O 复用
    a0 = chgdiff.read_chgcar(aeccar0_path)
    a2 = chgdiff.read_chgcar(aeccar2_path)
    if not chgdiff.same_grid(a0, a2):
        raise ValueError(
            f'AECCAR0 与 AECCAR2 网格不一致:{(a0["ngx"], a0["ngy"], a0["ngz"])} vs '
            f'{(a2["ngx"], a2["ngy"], a2["ngz"])};二者须来自同一计算,请勿混用不同作业')
    if not chgdiff.same_lattice(a0, a2):
        raise ValueError('AECCAR0 与 AECCAR2 晶格不一致;须来自同一计算,拒绝相加')
    total = [x + y for x, y in zip(a0['grid'], a2['grid'])]
    chgdiff.write_chgcar(a0, total, out_path)
    return {'out': str(out_path), 'n_grid': len(total),
            'ngx': a0['ngx'], 'ngy': a0['ngy'], 'ngz': a0['ngz']}


_BADER_INPUT_GUIDE = ('作业目录缺 {miss}:Bader 全电子口径需 LAECHG=.TRUE. 的静态作业'
                      "产出 AECCAR0/AECCAR2 并保留 CHGCAR。请先用 estatic.build_static_job"
                      "(purpose='bader') 生成静态作业、跑完后再回来分析。")


def run_bader(job_dir, *, exe: str | None = None, run=None, timeout: int = 600) -> dict:
    """跑完整 Bader 链:查输入 → 合 CHGCAR_sum → 调 bader → 解析 ACF.dat。

    - 缺 AECCAR0/AECCAR2/CHGCAR → ``{'ok': False, 'error': <需 LAECHG 静态作业>}``。
    - bader 不存在 → ``{'ok': False, 'error': '未找到 bader 可执行(Henkelman 组),
      请安装后重试;分析已准备好输入 CHGCAR_sum', 'chgcar_sum': <path>}``(不崩)。
    - 调用超时/非零退出/无 ACF.dat → ok=False + error(仍附 chgcar_sum)。
    - 成功 → ``{'ok': True, 'acf_path', 'chgcar_sum', 'charges', 'n_atoms'}``。
    run 可注入(测试);默认 subprocess.run(带 timeout 保护)。
    """
    import subprocess
    need = {name: os.path.join(job_dir, name)
            for name in ('AECCAR0', 'AECCAR2', 'CHGCAR')}
    miss = [name for name, path in need.items() if not os.path.isfile(path)]
    if miss:
        return {'ok': False, 'error': _BADER_INPUT_GUIDE.format(miss='/'.join(miss))}
    chgcar_sum = os.path.join(job_dir, 'CHGCAR_sum')
    try:
        sum_aeccar(need['AECCAR0'], need['AECCAR2'], chgcar_sum)
    except ValueError as e:
        return {'ok': False, 'error': f'合并 AECCAR0+AECCAR2 失败:{e}'}
    exe = exe or find_bader_exe()
    if not exe:
        return {'ok': False, 'chgcar_sum': chgcar_sum,
                'error': '未找到 bader 可执行(Henkelman 组),请安装后重试;'
                         '分析已准备好输入 CHGCAR_sum'}
    run = run or subprocess.run
    try:
        proc = run([exe, 'CHGCAR', '-ref', 'CHGCAR_sum'], cwd=job_dir,
                   timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True)
        code = getattr(proc, 'returncode', 1)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'ok': False, 'chgcar_sum': chgcar_sum, 'error': f'调用 bader 失败:{e}'}
    acf = os.path.join(job_dir, 'ACF.dat')
    if code != 0 or not os.path.isfile(acf):
        return {'ok': False, 'chgcar_sum': chgcar_sum,
                'error': f'bader 退出码 {code} 或未产出 ACF.dat(检查参考文件与磁盘)'}
    charges = parse_acf(acf)
    return {'ok': True, 'acf_path': acf, 'chgcar_sum': chgcar_sum,
            'charges': charges, 'n_atoms': len(charges)}
