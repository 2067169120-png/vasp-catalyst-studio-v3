"""Multiwfn 波函数分析静默驱动:探测 → 生成 stdin 命令流 → 调用 → 降级。

定位(对标 starpivot-DFT ⑤波函数分析):Multiwfn(卢天/Tian Lu)是把量子化学波函数
(.fchk/.wfn/.wfx/.molden)做实空间分析的瑞士军刀——ESP 表面极值(反应位点)、
HOMO/LUMO 与密度 cube(前线轨道/电荷分布)、NCI/IGMH(弱相互作用)、AIM 临界点
(键的量子拓扑证据)。这些是"催化剂为什么在这个位点吸附/活化"的电子结构证据链。

Multiwfn 是纯交互式命令行程序:主菜单输入功能编号 → 子菜单继续输入 → 逐层深入。
本模块把每个分析的"菜单编号序列"写成常量脚本,经 stdin 一次性喂入(Multiwfn xxx.wfn
< script),读回 stdout 与产物文件。对齐 povray_render / origin_charts 适配器口径:
- 纯函数(stdin 脚本生成 / ESP 极值解析)离线可测;
- subprocess 全程超时保护,测试经 monkeypatch 替身(不真跑 Multiwfn);
- 找不到 Multiwfn → 结构化 error + 把 stdin 脚本文本一并回传(用户可手动跑),绝不崩。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

# 可执行文件候选名(卢天官方发布为 Multiwfn / Multiwfn.exe;集群上常有 noGUI 变体)
_EXE_CANDIDATES = ('Multiwfn', 'Multiwfn.exe', 'Multiwfn_noGUI', 'multiwfn')

# Multiwfn 支持的波函数/波函数信息文件后缀(本驱动接受的输入格式)
_WAVEFN_EXTS = ('.fchk', '.fch', '.wfn', '.wfx', '.molden', '.mwfn')

# ── Multiwfn 菜单编号常量 ─────────────────────────────────────────────────────
# 交互式菜单编号按 Multiwfn 3.8 手册整理;不同版本菜单顺序可能微调。若版本差异导致
# 脚本喂错子菜单,请对照本机 Multiwfn 手册调整下列常量即可(脚本生成为纯函数,可改)。
_FUNC_ALIE = '18'           # 主功能5"实空间函数选择"里 ALIE(平均局域离子化能)的编号
_SUB_IRI = '4'              # 主功能20"弱相互作用可视化"里 IRI(相互作用区域指示函数)子功能号
_SURF_SELECT_MAPPED = '2'   # 主功能12"定量分子表面分析"里"选择映射函数"的子菜单项
_SURF_MAPPED_ALIE = '2'     # 上述"选择映射函数"子菜单里选 ALIE 的编号


# ── 探测 ─────────────────────────────────────────────────────────────────────
def probe(exe: str | None = None) -> dict:
    """探测 Multiwfn 可执行文件。返回 {'available','path','detail'}。

    exe 显式给定:是文件路径直接用;否则当作命令名在 PATH 找。
    exe 为 None:按候选名在 PATH 依次探测。全找不到 → available=False + 中文安装指引。
    """
    if exe:
        if os.path.isfile(exe):
            return {'available': True, 'path': exe, 'detail': f'使用指定的 Multiwfn:{exe}'}
        found = shutil.which(exe)
        if found:
            return {'available': True, 'path': found, 'detail': f'在 PATH 找到:{found}'}
        return {'available': False, 'path': None, 'detail': f'指定的 Multiwfn 不存在:{exe}'}
    for cand in _EXE_CANDIDATES:
        found = shutil.which(cand)
        if found:
            return {'available': True, 'path': found, 'detail': f'在 PATH 找到 Multiwfn:{found}'}
    return {'available': False, 'path': None,
            'detail': ('未找到 Multiwfn:请到 http://sobereva.com/multiwfn 下载,解压后把'
                       '程序目录加入 PATH,或调用时用 exe= 指定可执行文件路径。')}


# ── stdin 命令流生成(纯函数;每步含义随行注释) ──────────────────────────────────
def _orbital_token(params: dict | None) -> str:
    """轨道选择记号:整数编号原样转字符串;'homo'/'lumo'/'homo-1' 等标签统一大写。

    Multiwfn 在"输入轨道编号"处接受整数,也接受 HOMO/LUMO/HOMO-1/LUMO+2 之类关键字。
    """
    orb = (params or {}).get('orbital', 'HOMO')
    if isinstance(orb, int):
        return str(orb)
    return str(orb).upper()


def _script_esp_extrema(params: dict | None = None) -> str:
    """ESP 表面极值点(主功能 12 → 0)。"""
    lines = [
        '12',   # 主功能 12:定量分子表面分析(默认在 ρ=0.001 a.u. 等值面上分析静电势)
        '0',    # 子选项 0:立即用默认设置开始分析
        # 分析结束后 Multiwfn 在屏幕打印全局及各局部表面极小/极大点(kcal/mol);
        # run() 捕获 stdout,交 extrema_parse 解析。随后依赖 stdin EOF 退出程序。
    ]
    return '\n'.join(lines) + '\n'


def _script_density_cube(params: dict | None = None) -> str:
    """电子密度 cube(主功能 5 → 1)。"""
    grid = str((params or {}).get('grid', 2))
    lines = [
        '5',    # 主功能 5:计算并输出空间格点数据(cube)
        '1',    # 实空间函数 1:电子密度 ρ
        grid,   # 格点质量:1 低 / 2 中 / 3 高(默认中等)
        '2',    # 后处理选项 2:导出为 Gaussian 格式 cube 文件(density.cub)
        '0',    # 返回主菜单(随后 EOF 退出程序)
    ]
    return '\n'.join(lines) + '\n'


def _script_esp_cube(params: dict | None = None) -> str:
    """静电势 cube(主功能 5 → 12)。"""
    grid = str((params or {}).get('grid', 2))
    lines = [
        '5',    # 主功能 5:计算空间格点数据
        '12',   # 实空间函数 12:总静电势 ESP(计算较贵,可用 params['grid'] 下调格点)
        grid,   # 格点质量
        '2',    # 导出 cube(totesp.cub)
        '0',    # 返回主菜单
    ]
    return '\n'.join(lines) + '\n'


def _script_orbital_cube(params: dict | None = None) -> str:
    """HOMO/LUMO 等指定轨道 cube(主功能 5 → 4)。"""
    orb = _orbital_token(params)
    grid = str((params or {}).get('grid', 2))
    lines = [
        '5',    # 主功能 5:计算空间格点数据
        '4',    # 实空间函数 4:轨道波函数值
        orb,    # 轨道编号(整数)或 HOMO/LUMO/HOMO-1 等标签
        grid,   # 格点质量
        '2',    # 导出 cube(orbital.cub)
        '0',    # 返回主菜单
    ]
    return '\n'.join(lines) + '\n'


def _script_nci_rdg(params: dict | None = None) -> str:
    """NCI / RDG 弱相互作用分析(主功能 20 → 1)。"""
    grid = str((params or {}).get('grid', 2))
    lines = [
        '20',   # 主功能 20:弱相互作用可视化研究
        '1',    # 子功能 1:NCI 分析(RDG 约化密度梯度,Johnson 2010)
        grid,   # 格点质量;算完自动在当前目录写 func1.cub(sign(λ2)ρ)、func2.cub(RDG)
        '-10',  # 返回主菜单
    ]
    return '\n'.join(lines) + '\n'


def _script_igmh(params: dict | None = None) -> str:
    """IGMH 独立梯度模型(Hirshfeld 划分,主功能 20 → 11)。需原子分组 params['fragments']。"""
    frags = (params or {}).get('fragments')
    if not frags:
        raise ValueError("IGMH 需在 params 指定原子分组 fragments,"
                         "例如 {'fragments': ['1-12', '13-20']}")
    grid = str((params or {}).get('grid', 2))
    lines = ['20', '11', str(len(frags))]   # 主功能20 → 子功能11 IGMH;先给出片段数量
    for frag in frags:                      # 逐片段:Multiwfn 原子选择语法(如 1-5,7)
        lines.append(str(frag))
    lines.append(grid)                      # 格点质量;算完写 func1.cub、func2.cub
    lines.append('-10')                     # 返回主菜单
    return '\n'.join(lines) + '\n'


def _script_aim_cp(params: dict | None = None) -> str:
    """AIM 临界点拓扑分析(主功能 2)。"""
    lines = [
        '2',    # 主功能 2:拓扑分析(AIM / Bader QTAIM)
        '2',    # 从核位置搜索 CP(定位 (3,-3) 核吸引子 NCP)
        '3',    # 从原子对中点搜索 CP(定位 (3,-1) 键临界点 BCP)
        '7',    # 在指定/全部 CP 处输出实空间函数值
        '0',    # 输入 0 = 全部 CP → 把各 CP 的性质写出 CPprop.txt
        '-10',  # 返回主菜单
    ]
    return '\n'.join(lines) + '\n'


def _script_alie_cube(params: dict | None = None) -> str:
    """ALIE 平均局域离子化能 cube(主功能 5 → 实空间函数 ALIE)。"""
    grid = str((params or {}).get('grid', 2))
    lines = [
        '5',            # 主功能 5:计算并输出空间格点数据(cube)
        _FUNC_ALIE,     # 实空间函数:ALIE 平均局域离子化能(编号见文件顶部常量注释)
        grid,           # 格点质量:1 低 / 2 中 / 3 高
        '2',            # 后处理选项 2:导出 Gaussian 格式 cube(ALIE.cub)
        '0',            # 返回主菜单(随后 EOF 退出程序)
    ]
    return '\n'.join(lines) + '\n'


def _script_alie_extrema(params: dict | None = None) -> str:
    """ALIE 分子表面极值点(主功能 12,映射函数切为 ALIE → 0 开始)。

    ALIE 表面极小点 = 电子最易被移走处 = 亲电试剂进攻的敏感位点(亲电位点);
    与 ESP 极值同走"定量分子表面分析",只把映射函数从静电势换成 ALIE。
    """
    lines = [
        '12',                   # 主功能 12:定量分子表面分析(默认在 ρ=0.001 等值面上分析)
        _SURF_SELECT_MAPPED,    # 子菜单:选择映射函数(编号见文件顶部常量注释)
        _SURF_MAPPED_ALIE,      # 把映射函数选为 ALIE
        '0',                    # 立即开始分析;极小/极大点打印到 stdout,交 extrema_parse 解析
    ]
    return '\n'.join(lines) + '\n'


def _script_iri(params: dict | None = None) -> str:
    """IRI 相互作用区域指示函数(主功能 20 → IRI 子功能)。"""
    grid = str((params or {}).get('grid', 2))
    lines = [
        '20',        # 主功能 20:弱相互作用可视化研究
        _SUB_IRI,    # 子功能:IRI 相互作用区域指示函数分析(编号见文件顶部常量注释)
        grid,        # 格点质量;算完在当前目录写 func1.cub(IRI)、func2.cub(sign(λ2)ρ)
        '-10',       # 返回主菜单
    ]
    return '\n'.join(lines) + '\n'


# ── 分析项注册表 ─────────────────────────────────────────────────────────────
# key → {name(中文), stdin_script(fn(params)->str), outputs(期望产物默认文件名), note}
# outputs 用 Multiwfn 各功能的默认导出文件名;不同版本命名可能略有差异,run() 按名收集,
# 收不到时结构化报错并提示到 workdir 手查。
ANALYSES = {
    'esp_extrema': {
        'name': 'ESP 表面极值点',
        'stdin_script': _script_esp_extrema,
        'outputs': (),   # 无文件产物:极值列表打印在 stdout,用 extrema_parse 解析
        'note': ('定量分子表面分析(主功能12→0):在 ρ=0.001 等值面上求静电势极小/极大点,'
                 '定位亲电/亲核反应位点;结果在 stdout,交 extrema_parse 解析。'),
    },
    'homo_lumo_cube': {
        'name': 'HOMO/LUMO 轨道 cube',
        'stdin_script': _script_orbital_cube,
        'outputs': ('orbital.cub',),
        'note': ("主功能5→4:导出指定轨道的波函数值 cube。"
                 "params['orbital']=整数编号,或 'HOMO'/'LUMO'/'HOMO-1' 等标签。"),
    },
    'density_cube': {
        'name': '电子密度 cube',
        'stdin_script': _script_density_cube,
        'outputs': ('density.cub',),
        'note': '主功能5→1:导出电子密度 ρ 的 cube,供 VMD 画等值面。',
    },
    'esp_cube': {
        'name': '静电势 cube',
        'stdin_script': _script_esp_cube,
        'outputs': ('totesp.cub',),
        'note': '主功能5→12:导出总静电势 ESP 的 cube;与 density_cube 配对做 ESP 着色分子表面。',
    },
    'nci_rdg': {
        'name': 'NCI/RDG 弱相互作用',
        'stdin_script': _script_nci_rdg,
        'outputs': ('func1.cub', 'func2.cub'),
        'note': ('主功能20→1:NCI 分析,输出 func1.cub(sign(λ2)ρ)与 func2.cub(RDG),'
                 '供 VMD 画 nci 场景(氢键/vdW/位阻分色)。'),
    },
    'igmh': {
        'name': 'IGMH 独立梯度模型',
        'stdin_script': _script_igmh,
        'outputs': ('func1.cub', 'func2.cub'),
        'note': ("主功能20→11:基于 Hirshfeld 划分的 IGM,需 params['fragments'] 定义原子分组,"
                 '输出 func1.cub / func2.cub 表征片段间相互作用区域。'),
    },
    'aim_cp': {
        'name': 'AIM 临界点',
        'stdin_script': _script_aim_cp,
        'outputs': ('CPprop.txt',),
        'note': '主功能2:QTAIM 拓扑分析,搜核吸引子/键临界点,导出 CPprop.txt(键强/密度性质)。',
    },
    'alie': {
        'name': 'ALIE 平均局域离子化能 cube',
        'stdin_script': _script_alie_cube,
        'outputs': ('ALIE.cub',),
        'note': ('主功能5→实空间函数 ALIE:导出平均局域离子化能 cube。ALIE 低值区=电子'
                 '束缚弱=对亲电进攻敏感的位点;供 VMD alie_surface 着色分子表面。'
                 'ALIE 菜单号按 Multiwfn 3.8,版本差异见源码常量注释。'),
    },
    'alie_extrema': {
        'name': 'ALIE 表面极值点',
        'stdin_script': _script_alie_extrema,
        'outputs': (),   # 无文件产物:极值列表打印在 stdout,复用 extrema_parse 解析
        'note': ('定量分子表面分析(主功能12,映射函数切 ALIE→0):求 ALIE 表面极小/极大点。'
                 'ALIE 极小点=电子最易失去处=亲电位点;结果在 stdout,交 extrema_parse 解析'
                 '(数值单位为 eV,非 kcal/mol)。'),
    },
    'iri': {
        'name': 'IRI 相互作用区域指示函数',
        'stdin_script': _script_iri,
        'outputs': ('func1.cub', 'func2.cub'),
        'note': ('主功能20→IRI:相互作用区域指示函数,输出 func1.cub(IRI)与 func2.cub'
                 '(sign(λ2)ρ),既显化学键又显弱相互作用(比 NCI 更完整的相互作用视图);'
                 '供 VMD iri 场景。IRI 子功能号按 Multiwfn 3.8,版本差异见源码常量注释。'),
    },
}


# ── ESP 极值解析(纯函数) ────────────────────────────────────────────────────
def extrema_parse(text: str) -> dict:
    """解析 Multiwfn 定量分子表面分析(主功能12)的 ESP 表面极值输出。

    返回 {'minima': [{'value_kcal': float, 'xyz': [x,y,z]}, ...], 'maxima': [...]}。
    按包含 'minima'/'maxima'(或单数)的表头切分极小/极大两段,段内提取数据行
    「索引 + X Y Z + 数值(kcal/mol)」;容忍列宽与空白差异,汇总行/表头行自动跳过。
    """
    minima: list = []
    maxima: list = []
    bucket: list | None = None
    # 数据行:前导整数索引,后接 4 个浮点(X, Y, Z, 数值);汇总/表头行不满足此形状被跳过
    row_re = re.compile(
        r'^\s*\d+\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$')
    for line in text.splitlines():
        low = line.lower()
        if 'minima' in low or 'minimum' in low:
            bucket = minima
            continue
        if 'maxima' in low or 'maximum' in low:
            bucket = maxima
            continue
        if bucket is None:
            continue
        matched = row_re.match(line)
        if matched:
            x, y, z, val = (float(g) for g in matched.groups())
            bucket.append({'value_kcal': val, 'xyz': [x, y, z]})
    return {'minima': minima, 'maxima': maxima}


# ── 运行 ─────────────────────────────────────────────────────────────────────
def _tail(text: str, n: int = 60) -> str:
    """取文本末 n 行(stdout 尾部;ESP 极值等结果一般落在输出末段)。"""
    return '\n'.join(text.splitlines()[-n:])


def run(wavefn_file: str, analysis_key: str, *, exe: str | None = None,
        workdir: str | None = None, params: dict | None = None,
        timeout: int = 1800) -> dict:
    """跑一个 Multiwfn 分析。返回 {'ok','outputs','stdout_tail','elapsed_s','error'}。

    - wavefn_file:.fchk/.wfn/.wfx/.molden 等;analysis_key 见 ANALYSES。
    - stdin 脚本喂入 Multiwfn;产物按 ANALYSES[key]['outputs'] 收集,cube/txt 重命名带
      analysis 前缀(避免 func1.cub 这类默认名在同目录被不同分析互相覆盖)。
    - Multiwfn 缺失 → ok=False + 中文指引,并把 stdin 脚本文本放入 'script' 字段(用户
      可存成 script.txt 手动 `Multiwfn xxx.wfn < script.txt`)。全程超时保护,绝不抛。
    """
    if analysis_key not in ANALYSES:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'error': f'未知分析项 {analysis_key!r};可选:{", ".join(ANALYSES)}'}
    spec = ANALYSES[analysis_key]
    # 1) 先构建 stdin 脚本(参数错误在此暴露,并可随降级一并回传)
    try:
        script = spec['stdin_script'](params or {})
    except ValueError as e:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'error': str(e)}
    # 2) 波函数格式校验
    ext = os.path.splitext(str(wavefn_file))[1].lower()
    if ext not in _WAVEFN_EXTS:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'script': script,
                'error': f'不支持的波函数格式 {ext!r};支持:{", ".join(_WAVEFN_EXTS)}'}
    # 3) 探测 Multiwfn;缺失 → 降级并回传脚本文本
    info = probe(exe)
    if not info['available']:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'script': script,
                'error': (info['detail'] + ' 下面 script 字段是本分析的 Multiwfn 交互输入,'
                          f'可手动执行:Multiwfn {os.path.basename(str(wavefn_file))} < script.txt')}
    # 4) 输入文件存在性检查(Multiwfn 就绪后才检查,以便前面能优先回传脚本)
    if not os.path.isfile(wavefn_file):
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'script': script, 'error': f'波函数文件不存在:{wavefn_file}'}
    workdir = os.path.abspath(workdir) if workdir else os.path.dirname(os.path.abspath(str(wavefn_file)))
    os.makedirs(workdir, exist_ok=True)
    # 清理上一轮带前缀的历史产物(防收集到陈旧 cube 误判成功;不动原始默认名文件)
    for fname in spec['outputs']:
        stale = os.path.join(workdir, f'{analysis_key}_{fname}')
        if os.path.isfile(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
    wavefn_abs = os.path.abspath(str(wavefn_file))
    t0 = time.time()
    try:
        proc = subprocess.run([info['path'], wavefn_abs],
                              input=script.encode('utf-8'),
                              cwd=workdir, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        code = getattr(proc, 'returncode', 1)
        raw = getattr(proc, 'stdout', b'') or b''
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'script': script,
                'elapsed_s': round(time.time() - t0, 2),
                'error': f'Multiwfn 运行失败:{e}'}
    elapsed = round(time.time() - t0, 2)
    stdout_text = raw.decode('utf-8', errors='replace')
    tail = _tail(stdout_text)
    # 5) 收集产物:期望文件 → 带 analysis 前缀重命名
    collected: list = []
    for fname in spec['outputs']:
        src = os.path.join(workdir, fname)
        if os.path.isfile(src):
            dst = os.path.join(workdir, f'{analysis_key}_{fname}')
            try:
                os.replace(src, dst)
                collected.append(dst)
            except OSError:
                collected.append(src)
    # 6) 成功判定:有文件产物的看产物;无文件产物的(esp_extrema)看退出码或 stdout 结果标志
    if spec['outputs']:
        ok = bool(collected)
        err = '' if ok else 'Multiwfn 结束但未找到预期产物(版本导出名可能不同,请查 workdir)'
    else:
        low = stdout_text.lower()
        ok = code == 0 or ('minim' in low) or ('surface' in low)
        err = '' if ok else f'Multiwfn 退出码 {code},未见分析结果'
    return {'ok': ok, 'outputs': collected, 'stdout_tail': tail,
            'elapsed_s': elapsed, 'error': err}


def run_script(wavefn_file: str, stdin_script: str, outputs=(), *,
               exe: str | None = None, workdir: str | None = None,
               timeout: int = 1800) -> dict:
    """跑一段**现成 stdin 脚本**(逻辑同 run,但吃调用方给的菜单文本 + outputs 列表)。

    面向 api 层补充分析(_EXTRA_ANALYSES:ELF-LOL/ADCH/性质汇总/Fukui-CDFT)——这些的
    stdin 脚本在 api 侧生成,引擎只需照喂并按 outputs 收产物。返回同 run():
    ``{'ok','outputs','stdout_tail','elapsed_s','script'?,'error'}``。

    - wavefn_file:.fchk/.wfn/.wfx/.molden 等;stdin_script:已构建好的 Multiwfn 交互输入文本;
      outputs:期望产物默认文件名序列(空 → 看退出码/stdout 判成功,同无文件产物分析)。
    - Multiwfn 缺失 / 格式不支持 / 文件不存在 → ok=False 且把 'script' 回传(用户可手动
      ``Multiwfn xxx.wfn < script.txt``);全程超时保护,绝不抛。
    - 收产物时带 'extra_' 前缀重命名(避免 func1.cub 等默认名在同目录被不同分析互相覆盖)。
    """
    script = str(stdin_script or '')
    outs = tuple(outputs or ())
    if not script.strip():
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'error': 'stdin 脚本为空,无可执行的 Multiwfn 菜单流'}
    # 1) 波函数格式校验(脚本已现成,先校验输入格式)
    ext = os.path.splitext(str(wavefn_file))[1].lower()
    if ext not in _WAVEFN_EXTS:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'script': script,
                'error': f'不支持的波函数格式 {ext!r};支持:{", ".join(_WAVEFN_EXTS)}'}
    # 2) 探测 Multiwfn;缺失 → 降级并回传脚本文本(供用户手动运行)
    info = probe(exe)
    if not info['available']:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'script': script,
                'error': (info['detail'] + ' 下面 script 字段是本分析的 Multiwfn 交互输入,'
                          f'可手动执行:Multiwfn {os.path.basename(str(wavefn_file))} < script.txt')}
    # 3) 输入文件存在性检查(Multiwfn 就绪后才检查,以便前面能优先回传脚本)
    if not os.path.isfile(wavefn_file):
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'elapsed_s': 0.0,
                'script': script, 'error': f'波函数文件不存在:{wavefn_file}'}
    workdir = os.path.abspath(workdir) if workdir else os.path.dirname(os.path.abspath(str(wavefn_file)))
    os.makedirs(workdir, exist_ok=True)
    # 清理上一轮带前缀的历史产物(防收集到陈旧 cube 误判成功;不动原始默认名文件)
    for fname in outs:
        stale = os.path.join(workdir, f'extra_{fname}')
        if os.path.isfile(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
    wavefn_abs = os.path.abspath(str(wavefn_file))
    t0 = time.time()
    try:
        proc = subprocess.run([info['path'], wavefn_abs],
                              input=script.encode('utf-8'),
                              cwd=workdir, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        code = getattr(proc, 'returncode', 1)
        raw = getattr(proc, 'stdout', b'') or b''
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'ok': False, 'outputs': [], 'stdout_tail': '', 'script': script,
                'elapsed_s': round(time.time() - t0, 2),
                'error': f'Multiwfn 运行失败:{e}'}
    elapsed = round(time.time() - t0, 2)
    stdout_text = raw.decode('utf-8', errors='replace')
    tail = _tail(stdout_text)
    # 收集产物:期望文件 → 带 extra_ 前缀重命名
    collected: list = []
    for fname in outs:
        src = os.path.join(workdir, fname)
        if os.path.isfile(src):
            dst = os.path.join(workdir, f'extra_{fname}')
            try:
                os.replace(src, dst)
                collected.append(dst)
            except OSError:
                collected.append(src)
    # 成功判定:有文件产物的看产物;无文件产物的(如 ADCH/性质汇总)看退出码
    if outs:
        ok = bool(collected)
        err = '' if ok else 'Multiwfn 结束但未找到预期产物(版本导出名可能不同,请查 workdir)'
    else:
        ok = code == 0
        err = '' if ok else f'Multiwfn 退出码 {code},未见分析结果'
    return {'ok': ok, 'outputs': collected, 'stdout_tail': tail,
            'elapsed_s': elapsed, 'error': err}
