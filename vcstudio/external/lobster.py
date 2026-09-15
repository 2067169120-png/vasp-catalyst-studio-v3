"""LOBSTER/COHP 全链适配器:lobsterin 生成 → 前置检查 → 探测调用 → 输出解析 → 出图。

定位(2026-07):LOBSTER 把 VASP 平面波波函数投影回局域原子基,产出 COHP
(Crystal Orbital Hamilton Population)——键的成键/反键分辨与 ICOHP 键强定量,是
催化剂"为什么这个位点吸附强"的电子结构证据链末端。本模块对齐 povray_render /
origin_charts 的适配器口径:纯函数(lobsterin 文本 / COHPCAR 解析 / spilling 闸)
离线可测,subprocess 经 run= 注入替身,找不到 lobster → 结构化 error 不抛。

符号与口径约定(报告/方法学节须同款措辞):
- COHP 原始量子化学惯例:**COHP<0 = 成键、COHP>0 = 反键**(能量降低利于成键)。
- 绘图惯例 −pCOHP:cohp_plot(flip=True) 画 −COHP,使**成键落在正值(右侧)**,
  这是 LOBSTER/wxDragon 出版图的通行画法;能量轴取 E−E_F(费米零点为水平线)。
- ICOHP:COHP 对能量的积分(积到费米能级),**ICOHP<0 = 净成键,越负键越强**;
  非自旋计算的 ICOHP 已含两个自旋道(因子 2),自旋极化则上/下分列、总量=上+下。
- LOBSTER 依 VASP ISPIN 自动决定自旋分辨,lobsterin 无自旋开关(spin 参数仅落注释)。
- 投影质量以 spilling(投影残差)度量:absolute total spilling >5% 则投影不可信
  (COHP 失真),须换 basis 或加 NBANDS 重跑——spilling_gate 落此判据。

matplotlib 延迟到 cohp_plot 内 import(顶层零重依赖);风格复用 native_charts 规范层。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

# ── lobsterin 生成 ───────────────────────────────────────────────────────────

def lobsterin_text(*, atom_pairs=None, distance_range=(0.5, 3.0),
                   basis: str = 'pbeVaspFit2015', e_window=(-10, 5),
                   spin: bool = True) -> str:
    """生成 lobsterin 输入文本(逐行含中文注释说明)。

    参数:
        atom_pairs: [(i, j), ...] 指定原子对(**1 起**,对齐 POSCAR/vasprun 原子序);
                    给出则逐对写 ``cohpbetween atom i atom j``,不再自动枚举。
        distance_range: (lo, hi) Å;atom_pairs 为 None 时用 ``cohpGenerator from lo to hi``
                    自动枚举该距离区间内的成键原子对。
        basis: 基组名(pbeVaspFit2015 / bunge / koga);写 ``basisSet``。
        e_window: (lo, hi) eV(相对费米能级)→ COHPstartEnergy / COHPendEnergy。
        spin: 仅落注释——LOBSTER 依 VASP ISPIN 自动输出自旋分辨 COHP,lobsterin 无开关。

    返回:lobsterin 全文本(末尾含换行)。
    """
    lo_e, hi_e = float(e_window[0]), float(e_window[1])
    lines = [
        '# LOBSTER 输入文件(vcstudio 自动生成)——把 VASP 波函数投影回局域原子基算 COHP',
        '# COHP 能量窗口(相对费米能级,eV):只在此窗口内输出 COHP/DOS',
        f'COHPstartEnergy {lo_e:g}',
        f'COHPendEnergy {hi_e:g}',
        f'# 局域基组:{basis}(VASP PAW → Slater 型原子基的拟合系数集)',
        f'basisSet {basis}',
        '# 高斯展宽(eV):COHP/DOS 能量涂抹宽度,与静态 SIGMA 量级一致即可',
        'gaussianSmearingWidth 0.05',
    ]
    # 自旋:LOBSTER 无 lobsterin 开关,依 ISPIN 自动;此处仅记录预期,便于溯源
    if spin:
        lines.append('# 自旋:VASP ISPIN=2 时 LOBSTER 自动输出上/下自旋分辨 COHP(无需在此开关)')
    else:
        lines.append('# 自旋:按非自旋极化处理(VASP ISPIN=1;如需自旋分辨请以 ISPIN=2 重跑静态)')
    lines.append('# —— COHP 原子对选择 ——')
    if atom_pairs:
        lines.append('# 指定原子对(atom 序号 1 起,对齐 POSCAR 原子顺序):')
        for pair in atom_pairs:
            i, j = int(pair[0]), int(pair[1])
            lines.append(f'cohpbetween atom {i} atom {j}')
    else:
        lo_d, hi_d = float(distance_range[0]), float(distance_range[1])
        lines.append(f'# 按距离区间自动枚举成键对({lo_d:g}–{hi_d:g} Å 内的近邻):')
        lines.append(f'cohpGenerator from {lo_d:g} to {hi_d:g}')
    return '\n'.join(lines) + '\n'


# ── 静态作业前置检查 + 写 lobsterin ─────────────────────────────────────────

# pbeVaspFit2015 局域基每原子的基函数数估算(NBANDS 下限用):
# 主族按价壳 s/sp,过渡金属按 (n-1)d ns np=9,缺登记回落 9(保守偏大)。
_BASIS_NBASIS = {
    'H': 1, 'He': 1,
    'Li': 4, 'Be': 4, 'B': 4, 'C': 4, 'N': 4, 'O': 4, 'F': 4, 'Ne': 4,
    'Na': 4, 'Mg': 4, 'Al': 4, 'Si': 4, 'P': 4, 'S': 4, 'Cl': 4, 'Ar': 4,
    'K': 4, 'Ca': 4,
    'Sc': 9, 'Ti': 9, 'V': 9, 'Cr': 9, 'Mn': 9, 'Fe': 9, 'Co': 9,
    'Ni': 9, 'Cu': 9, 'Zn': 9,
    'Ga': 4, 'Ge': 4, 'As': 4, 'Se': 4, 'Br': 4,
    'Y': 9, 'Zr': 9, 'Nb': 9, 'Mo': 9, 'Ru': 9, 'Rh': 9, 'Pd': 9,
    'Ag': 9, 'Cd': 9,
    'Hf': 9, 'Ta': 9, 'W': 9, 'Re': 9, 'Os': 9, 'Ir': 9, 'Pt': 9, 'Au': 9,
}
_DEFAULT_NBASIS = 9
_INT_RE = re.compile(r'[-+]?\d+')


def _estimate_min_nbands(job_dir: str) -> int | None:
    """据 POSCAR/CONTCAR 元素×计数估算 LOBSTER 所需最小 NBANDS(基函数总数)。

    读不到结构 → None(调用方降级为"无法核验 NBANDS")。
    """
    try:
        from vcstudio.generate.poscar import parse_poscar_species
    except ImportError:
        return None
    text = None
    for name in ('CONTCAR', 'POSCAR'):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            try:
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
                break
            except OSError:
                continue
    if text is None:
        return None
    els, counts = parse_poscar_species(text)
    if not els or not counts or len(els) != len(counts):
        return None
    return sum(_BASIS_NBASIS.get(el, _DEFAULT_NBASIS) * int(n)
               for el, n in zip(els, counts))


def prepare_lobster_dir(job_dir, **kw) -> dict:
    """在完成的静态作业目录写 lobsterin,并检查 LOBSTER 前置条件。

    kw 透传 lobsterin_text(atom_pairs/distance_range/basis/e_window/spin)。

    返回 ``{'ok','lobsterin_path','requirements':[...],'error'}``:
        - requirements:中文缺项提示列表(缺 WAVECAR/vasprun.xml/POTCAR、ISYM≠−1、
          NBANDS 不足);非空即"尚不能直接跑 LOBSTER"。
        - ok:requirements 为空(前置齐备)时 True;lobsterin 无论如何都尝试写出。
        - error:硬失败(目录不存在/写盘失败)时非空,此时 ok=False。
    """
    job_dir = str(job_dir)
    if not os.path.isdir(job_dir):
        return {'ok': False, 'lobsterin_path': None, 'requirements': [],
                'error': f'作业目录不存在:{job_dir}'}
    try:
        text = lobsterin_text(**kw)
    except (TypeError, ValueError) as e:
        return {'ok': False, 'lobsterin_path': None, 'requirements': [],
                'error': f'lobsterin 生成失败:{e}'}
    lobsterin_path = os.path.join(job_dir, 'lobsterin')
    try:
        with open(lobsterin_path, 'w', encoding='utf-8') as f:
            f.write(text)
    except OSError as e:
        return {'ok': False, 'lobsterin_path': None, 'requirements': [],
                'error': f'写 lobsterin 失败:{e}'}

    requirements: list[str] = []
    # 1) 前置文件:LOBSTER 需波函数(WAVECAR)+ vasprun.xml(能级/费米)+ POTCAR(投影身份)
    if not os.path.isfile(os.path.join(job_dir, 'WAVECAR')):
        requirements.append('缺 WAVECAR:LOBSTER 需波函数,静态须设 LWAVE=.TRUE. 重跑')
    if not os.path.isfile(os.path.join(job_dir, 'vasprun.xml')):
        requirements.append('缺 vasprun.xml:LOBSTER 需其读能级/费米能/基组信息')
    if not os.path.isfile(os.path.join(job_dir, 'POTCAR')):
        requirements.append('缺 POTCAR:LOBSTER 依赖赝势身份匹配局域基组')

    # 2) INCAR:ISYM=-1(关对称,LOBSTER 要求全 k 点)+ 足够 NBANDS(≥基函数数)
    incar_path = os.path.join(job_dir, 'INCAR')
    min_nbands = _estimate_min_nbands(job_dir)
    if os.path.isfile(incar_path):
        try:
            from vcstudio.generate.incar_builder import parse_incar
            with open(incar_path, 'r', encoding='utf-8', errors='replace') as f:
                incar = parse_incar(f.read())
        except (OSError, ValueError, ImportError):
            incar = None
        if incar is not None:
            isym = incar.get('ISYM')
            if _first_int(isym) not in (-1, 0):
                nb = f',NBANDS≥{min_nbands}' if min_nbands else ''
                requirements.append(
                    f'ISYM={isym!r}≠-1:LOBSTER 要求关对称,需重跑静态:'
                    f'ISYM=-1,LWAVE=.TRUE.{nb}')
            cur_nbands = _first_int(incar.get('NBANDS'))
            if min_nbands is not None:
                if cur_nbands is None:
                    requirements.append(
                        f'INCAR 未设 NBANDS:LOBSTER 需 NBANDS≥基函数数(≈{min_nbands}),'
                        '建议显式设 NBANDS 后重跑(VASP 默认可能不足)')
                elif cur_nbands < min_nbands:
                    requirements.append(
                        f'NBANDS={cur_nbands}<基函数数≈{min_nbands}:投影空间不足,'
                        f'需重跑静态:ISYM=-1,LWAVE=.TRUE.,NBANDS≥{min_nbands}')
    else:
        requirements.append('缺 INCAR:无法核验 ISYM/NBANDS,请确认静态设 ISYM=-1、LWAVE=.TRUE.')

    return {'ok': not requirements, 'lobsterin_path': lobsterin_path,
            'requirements': requirements, 'error': ''}


def _first_int(v):
    """从 INCAR 值(int/'−1'/'0.0'/'2 2 2')取首个整数;取不到 → None。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v == int(v) else None
    m = _INT_RE.search(str(v))
    return int(m.group(0)) if m else None


# ── 探测与调用 ───────────────────────────────────────────────────────────────
_LOBSTER_NAMES = ('lobster', 'lobster-5.1.1', 'lobster-5.1.0', 'lobster-5.0.0',
                  'lobster-4.1.0', 'lobster-4.0.0')
_DEFAULT_LOBSTER_DIRS = ('/usr/local/bin', '/opt/lobster/bin',
                         os.path.expanduser('~/bin'))


def find_lobster(configured: str = '') -> str | None:
    """lobster 可执行探测:配置路径 → PATH(常见版本名)→ 默认安装位。找不到 → None。"""
    if configured and os.path.isfile(configured):
        return configured
    for name in _LOBSTER_NAMES:
        hit = shutil.which(name)
        if hit:
            return hit
    for d in _DEFAULT_LOBSTER_DIRS:
        for name in _LOBSTER_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


_INSTALL_HINT = ('未找到 lobster 可执行文件:请从 http://www.cohp.de 下载 LOBSTER '
                 '(免费学术许可),解压后把可执行文件放入 PATH 或在配置中指定路径')


def run_lobster(job_dir, exe: str | None = None, timeout: int = 1800,
                configured_exe: str = '', run=None) -> dict:
    """在作业目录本地调用 lobster(读该目录 lobsterin)。返回 {'ok','stdout_tail','error'}。

    - 找不到可执行文件 → ok=False + 安装指引(不抛)。
    - 超时/非零退出/异常 → ok=False + error 文本(全自动链路里单步失败可跳过)。
    - run 可注入(测试);默认 subprocess.run。
    """
    run = run or subprocess.run
    exe = exe or find_lobster(configured_exe)
    if not exe:
        return {'ok': False, 'stdout_tail': '', 'error': _INSTALL_HINT}
    job_dir = str(job_dir)
    if not os.path.isfile(os.path.join(job_dir, 'lobsterin')):
        return {'ok': False, 'stdout_tail': '',
                'error': f'作业目录缺 lobsterin:{job_dir}(先调 prepare_lobster_dir)'}
    try:
        proc = run([exe], cwd=job_dir, timeout=timeout,
                   stdin=subprocess.DEVNULL, capture_output=True)
        code = getattr(proc, 'returncode', 1)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'ok': False, 'stdout_tail': '', 'error': f'lobster 调用失败:{e}'}
    stdout = getattr(proc, 'stdout', b'') or b''
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', errors='replace')
    tail = stdout.strip()[-2000:]
    if code != 0:
        return {'ok': False, 'stdout_tail': tail, 'error': f'lobster 退出码 {code}'}
    # LOBSTER 不靠退出码报投影失败,产出 lobsterout/COHPCAR 才算成功
    ok = os.path.isfile(os.path.join(job_dir, 'lobsterout')) or \
        os.path.isfile(os.path.join(job_dir, 'COHPCAR.lobster'))
    return {'ok': ok, 'stdout_tail': tail,
            'error': '' if ok else 'lobster 退出 0 但未见 lobsterout/COHPCAR 产出'}


# ── COHPCAR.lobster 解析 ─────────────────────────────────────────────────────

def _is_numeric_row(line: str) -> bool:
    """整行是否全为可解析浮点(COHPCAR 数据行判定)。"""
    toks = line.split()
    if not toks:
        return False
    try:
        for t in toks:
            float(t)
    except ValueError:
        return False
    return True


def _parse_cohp_label(line: str) -> tuple:
    """'No.1:Fe1->O2(2.05)[3d-2p]' → ('Fe1->O2', 2.05)。取不到距离 → (label, None)。"""
    s = line.strip()
    if ':' in s:
        s = s.split(':', 1)[1]
    dist = None
    m = re.search(r'\(([-\d.]+)\)', s)
    if m:
        try:
            dist = float(m.group(1))
        except ValueError:
            dist = None
    label = re.sub(r'\(.*', '', s).strip()   # 去掉 (dist)[orbital] 尾巴
    return label or s.strip(), dist


def parse_cohpcar(path) -> dict:
    """解析 COHPCAR.lobster → 平均 + 各原子对的 COHP/ICOHP(自旋分辨时双块)。

    返回::

        {'energies': [...], 'efermi': float, 'efermi_zeroed': bool,
         'spin_polarized': bool,
         'pairs': [{'label','distance','cohp':[...],'icohp':[...],
                    'cohp_up','cohp_down','icohp_up','icohp_down'}, ...]}

    - pairs[0] 为 'average'(全对平均),其后为各命名对。
    - cohp 为**自旋合计**(非自旋即原值,自旋极化=上+下);cohp_up/cohp_down 仅自旋
      极化时非 None(icohp 同理)。
    - 能量列为 LOBSTER 已平移到费米零点的 E−E_F;efermi_zeroed 反映末参数是否≈0。
    - 坏/截断文件、列数不符 → ValueError。
    """
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        raw = [ln.rstrip('\n') for ln in f]
    lines = [ln for ln in raw if ln.strip()]
    if len(lines) < 3:
        raise ValueError('COHPCAR.lobster 内容过短或为空(文件损坏/截断)')
    params = lines[1].split()
    try:
        n_entries = int(params[0])          # = 平均(1)+ 对数
        spin = int(params[1])               # 1 非自旋 / 2 自旋极化
        efermi = float(params[-1])
    except (ValueError, IndexError) as e:
        raise ValueError(f'COHPCAR.lobster 参数行解析失败:{lines[1]!r}({e})')
    if n_entries < 1 or spin not in (1, 2):
        raise ValueError(f'COHPCAR.lobster 参数异常:n_entries={n_entries}, spin={spin}')
    num_bonds = n_entries - 1

    # 从参数行后收集标签行(非数字行),遇首个数字行进入数据段
    labels = []
    idx = 2
    while idx < len(lines) and not _is_numeric_row(lines[idx]):
        labels.append(_parse_cohp_label(lines[idx]))
        idx += 1
    data_lines = lines[idx:]
    if not data_lines:
        raise ValueError('COHPCAR.lobster 无数据行(文件截断?)')

    ncol_expected = 1 + 2 * n_entries * spin
    rows = []
    for ln in data_lines:
        toks = ln.split()
        if len(toks) < ncol_expected:
            raise ValueError(
                f'COHPCAR.lobster 数据行列数 {len(toks)}<预期 {ncol_expected}'
                f'(n_entries={n_entries}, spin={spin});文件损坏或格式不符')
        try:
            rows.append([float(t) for t in toks[:ncol_expected]])
        except ValueError as e:
            raise ValueError(f'COHPCAR.lobster 数据行含非数字:{ln!r}({e})')
    cols = list(zip(*rows))                 # 转置:cols[c] 为第 c 列全能量点
    energies = list(cols[0])

    def _col(c):
        return list(cols[c])

    def _entry(block_pos):
        """按块内位置取一条 COHP/ICOHP(合计 + 自旋分辨)。block_pos:0=平均,b+1=第 b 对。"""
        base = 1 + 2 * block_pos            # 上自旋 COHP 列
        up_c, up_i = _col(base), _col(base + 1)
        if spin == 1:
            return {'cohp': up_c, 'icohp': up_i,
                    'cohp_up': None, 'cohp_down': None,
                    'icohp_up': None, 'icohp_down': None}
        dbase = 1 + 2 * n_entries + 2 * block_pos
        dn_c, dn_i = _col(dbase), _col(dbase + 1)
        return {'cohp': [a + b for a, b in zip(up_c, dn_c)],
                'icohp': [a + b for a, b in zip(up_i, dn_i)],
                'cohp_up': up_c, 'cohp_down': dn_c,
                'icohp_up': up_i, 'icohp_down': dn_i}

    pairs = []
    avg = {'label': 'average', 'distance': None}
    avg.update(_entry(0))
    pairs.append(avg)
    for b in range(num_bonds):
        label, dist = labels[b] if b < len(labels) else (f'pair{b + 1}', None)
        d = {'label': label, 'distance': dist}
        d.update(_entry(b + 1))
        pairs.append(d)

    return {'energies': energies, 'efermi': efermi,
            'efermi_zeroed': abs(efermi) < 1e-6,
            'spin_polarized': spin == 2, 'pairs': pairs}


# ── ICOHPLIST.lobster 解析 ───────────────────────────────────────────────────

def parse_icohplist(path) -> list:
    """解析 ICOHPLIST.lobster → 各对的 ICOHP(积到费米,键强度)。

    返回 ``[{'pair','atom_a','atom_b','icohp','icohp_up','icohp_down','distance'}, ...]``:
        - 非自旋:icohp 为该值,icohp_up/down=None。
        - 自旋极化(上块+下块):icohp_up/icohp_down 分列,icohp=上+下(总键强)。
    ICOHP<0 = 净成键、越负键越强。坏/空文件 → ValueError。
    """
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        raw = [ln.rstrip('\n') for ln in f]
    lines = [ln for ln in raw if ln.strip()]
    if len(lines) < 2:
        raise ValueError('ICOHPLIST.lobster 内容过短或为空(文件损坏/截断)')
    # 跳过首行表头;逐行解析(label a1 a2 distance [tx ty tz] ICOHP)
    recs = []
    for ln in lines[1:]:
        toks = ln.split()
        if len(toks) < 5:
            continue
        try:
            dist = float(toks[3])
            icohp = float(toks[-1])
        except ValueError:
            continue
        recs.append({'atom_a': toks[1], 'atom_b': toks[2],
                     'pair': f'{toks[1]}->{toks[2]}', 'distance': dist,
                     'icohp': icohp})
    if not recs:
        raise ValueError('ICOHPLIST.lobster 无有效数据行(表头之后为空或格式不符)')

    # 自旋极化检测:偶数行且前半/后半的 (a1,a2) 序列一致 → 上块+下块
    half = len(recs) // 2
    spin_polarized = (len(recs) % 2 == 0 and half > 0 and
                      all(recs[i]['pair'] == recs[i + half]['pair']
                          for i in range(half)))
    out = []
    if spin_polarized:
        for i in range(half):
            up, dn = recs[i], recs[i + half]
            out.append({'pair': up['pair'], 'atom_a': up['atom_a'],
                        'atom_b': up['atom_b'], 'distance': up['distance'],
                        'icohp_up': up['icohp'], 'icohp_down': dn['icohp'],
                        'icohp': up['icohp'] + dn['icohp']})
    else:
        for r in recs:
            out.append({'pair': r['pair'], 'atom_a': r['atom_a'],
                        'atom_b': r['atom_b'], 'distance': r['distance'],
                        'icohp_up': None, 'icohp_down': None,
                        'icohp': r['icohp']})
    return out


# ── spilling(投影质量)解析 + 质量闸 ────────────────────────────────────────
_TOTAL_SPILL_RE = re.compile(r'total spilling[^\d%]*([\d.]+)\s*%', re.IGNORECASE)
_CHARGE_SPILL_RE = re.compile(r'charge spilling[^\d%]*([\d.]+)\s*%', re.IGNORECASE)


def parse_spilling(lobsterout_path) -> dict:
    """解析 lobsterout 的 spilling(投影残差)→ 分数(非百分数)。

    返回 ``{'abs_total','charge_spilling','per_spin':[{'total','charge'}...]}``:
        - abs_total / charge_spilling 为**分数**(如 0.05 = 5%),取各自旋道最差值。
        - per_spin 按出现顺序保留每自旋道 (total, charge)。
    未找到任何 spilling 行 → ValueError。
    """
    with open(lobsterout_path, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()
    totals = [float(x) / 100.0 for x in _TOTAL_SPILL_RE.findall(text)]
    charges = [float(x) / 100.0 for x in _CHARGE_SPILL_RE.findall(text)]
    if not totals and not charges:
        raise ValueError('lobsterout 未找到 spilling 信息(文件不完整或非 LOBSTER 日志)')
    per_spin = []
    for i in range(max(len(totals), len(charges))):
        per_spin.append({'total': totals[i] if i < len(totals) else None,
                         'charge': charges[i] if i < len(charges) else None})
    return {'abs_total': max(totals) if totals else None,
            'charge_spilling': max(charges) if charges else None,
            'per_spin': per_spin}


def spilling_gate(spilling: dict, threshold: float = 0.05) -> dict:
    """投影质量闸:abs_total(或 charge)>阈值 → 不通过 + 中文告警。

    返回 ``{'ok','note'}``。threshold 默认 0.05(5%,LOBSTER 通行经验线)。
    """
    vals = [v for v in (spilling.get('abs_total'), spilling.get('charge_spilling'))
            if v is not None]
    if not vals:
        return {'ok': False, 'note': 'spilling 缺 total/charge 数值,无法判定投影质量'}
    worst = max(vals)
    if worst > threshold:
        return {'ok': False,
                'note': (f'投影残差 {worst * 100:.1f}%>{threshold * 100:.0f}%:'
                         '投影质量差,COHP 不可信,考虑换 basisSet 或加 NBANDS 重跑静态')}
    return {'ok': True,
            'note': f'投影残差 {worst * 100:.1f}%≤{threshold * 100:.0f}%,投影质量可接受'}


# ── 出版级 COHP 图(−pCOHP vs E−E_F)────────────────────────────────────────

def cohp_plot(parsed: dict, out_path, *, pair_labels=None, flip: bool = True,
              icohp_annotate: bool = True, formats=('png', 'pdf')) -> list:
    """出版级 COHP 图:x=−pCOHP(成键正值在右)、y=E−E_F,费米零线 + ICOHP 标注。

    数据契约:parsed 为 parse_cohpcar 的返回。pair_labels 为 None → 画除 'average'
    外的全部命名对(仅有 average 时画 average);给列表则只画匹配 label 的对。
    flip=True 画 −COHP(LOBSTER 出版惯例:成键落正值/右侧);自旋分辨时上自旋实线、
    下自旋虚线。icohp_annotate=True 在费米能级处标注各对 ICOHP(键强)。

    风格复用 native_charts 规范层(apply_paper_style/_new_figure/_save_dual);
    matplotlib 延迟 import。返回导出文件绝对路径列表。parsed 结构异常 → ValueError。
    """
    from vcstudio.external import native_charts as nc

    energies = parsed.get('energies')
    all_pairs = parsed.get('pairs') or []
    if not energies or not all_pairs:
        raise ValueError('parsed 缺 energies 或 pairs(先用 parse_cohpcar 解析)')
    named = [p for p in all_pairs if p.get('label') != 'average']
    pool = named or all_pairs
    if pair_labels is not None:
        want = {str(x) for x in pair_labels}
        sel = [p for p in all_pairs if str(p.get('label')) in want]
        if not sel:
            raise ValueError(f'pair_labels {sorted(want)} 未匹配到任何对'
                             f'(可用:{[p.get("label") for p in all_pairs]})')
    else:
        sel = pool
    sign = -1.0 if flip else 1.0

    # 费米能级(y=0)最近的能量索引 → ICOHP 键强取值处
    f_idx = min(range(len(energies)), key=lambda i: abs(energies[i]))

    with nc.apply_paper_style():
        fig, ax = nc._new_figure(width=nc.SINGLE_COL, aspect=1.28)
        colors = nc.PALETTES['tol_bright']
        for i, p in enumerate(sel):
            c = colors[i % len(colors)]
            label = nc.chem_label(p.get('label', f'pair{i + 1}'))
            if p.get('cohp_up') is not None and p.get('cohp_down') is not None:
                ax.plot([sign * v for v in p['cohp_up']], energies, color=c,
                        lw=1.3, zorder=3, label=label)
                ax.plot([sign * v for v in p['cohp_down']], energies, color=c,
                        lw=1.0, ls=(0, (3, 2)), zorder=3)
            else:
                ax.plot([sign * v for v in p['cohp']], energies, color=c,
                        lw=1.3, zorder=3, label=label)
            if icohp_annotate:
                icv = p['icohp'][f_idx]
                ax.annotate(rf'ICOHP={icv:.2f}', (0.03, 0.02 + 0.05 * i),
                            xycoords='axes fraction', ha='left', va='bottom',
                            fontsize=6.5, color=c, zorder=5)
        ax.axhline(0.0, color=nc.ZERO_LINE_COLOR, lw=0.9, ls=(0, (5, 3)), zorder=2)
        ax.axvline(0.0, color=nc.ZERO_LINE_COLOR, lw=0.8, zorder=1)
        ax.annotate(r'$E_\mathrm{F}$', (1.0, 0.0), xycoords=('axes fraction', 'data'),
                    xytext=(-3, 2), textcoords='offset points', ha='right',
                    va='bottom', fontsize=7.5, color=nc.ZERO_LINE_COLOR)
        # 成键/反键侧提示(flip 时右侧为成键)
        bond_txt = ('antibonding', 'bonding') if flip else ('bonding', 'antibonding')
        ax.text(0.02, 0.985, bond_txt[0], transform=ax.transAxes, ha='left',
                va='top', fontsize=7, fontstyle='italic', color='#777777')
        ax.text(0.98, 0.985, bond_txt[1], transform=ax.transAxes, ha='right',
                va='top', fontsize=7, fontstyle='italic', color='#777777')
        ax.set_ylabel(r'$E - E_\mathrm{F}$ (eV)')
        ax.set_xlabel(r'$-$pCOHP' if flip else 'pCOHP')
        if any(p.get('label') != 'average' for p in sel) or pair_labels:
            ax.legend(loc='upper right', fontsize=6.5)
        return nc._save_dual(fig, out_path, formats)
