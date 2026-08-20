"""AIMD(从头算分子动力学)作业生成端——补全"计算种类"里的热稳定性一环。

对标图表菜单里的 E3(AIMD 热稳定性图):单原子/团簇在有限温度下会不会团聚脱落,是审稿人
常问的稳定性质疑,标准做法是跑一段 NVT AIMD(论文第4章口径:Nose-Hoover 300 K、10 ps、
1 fs 步长),看能量-时间曲线是否平稳、首尾构型是否保持。本模块从**完成弛豫**的作业目录一键
派生 AIMD 输入(CONTCAR→POSCAR + 由源 INCAR 派生 MD INCAR + Γ 点 KPOINTS + 原样 POTCAR),
并解析 OSZICAR 的 MD 行供出能量-时间曲线。

设计原则(与 freq_builder 一致):
- **只改必须改的**:MD INCAR 从源 INCAR 派生,仅替换/新增 MD 必需键(IBRION/NSW/POTIM/
  MDALGO/SMASS/TEBEG/TEEND/ISYM/NELMIN),剥离与 MD 冲突的离子弛豫/变胞键(EDIFFG/ISIF);电子学
  参数(ENCUT/GGA/ISPIN/MAGMOM/IVDW/LDAU*)**原样保留**,逐条注明改动(输出含 changes 清单
  + INCAR 注释)。ENCUT 不自动下调(除非显式传 encut)。
- **绝不静默猜**:CONTCAR 缺失退回 POSCAR、ENCUT 未降、Γ 点采样等都以中文 warning 显式暴露。
- 纯函数为主,复用 poscar/incar_builder 既有解析,不另造 parser。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re
import shutil
import math
from collections import OrderedDict
from pathlib import Path

from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.kpoints import kpoints_str
from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.shared import manifest as manifest_mod

ENSEMBLES = ('nvt', 'nve')

# 与 MD 冲突的键 → 剥离(IBRION 走 replace,不在此):
#   EDIFFG 是离子弛豫力/能收敛判据,MD 跑满 NSW 步与它无关,留着误导;
#   ISIF(应力/晶胞优化)对固定胞的 NVT/NVE AIMD 无意义(IBRION=0 下 VASP 忽略),
#   留 ISIF=3 会让人误以为在变胞。与 freq_builder 剥离集同口径。
_AIMD_STRIP = ('ISIF', 'EDIFFG', 'LANGEVIN_GAMMA', 'LANGEVIN_GAMMA_L', 'PMASS')

_AIMD_REASON = {
    'IBRION': 'IBRION=0 开启分子动力学',
    'NSW': 'MD 总步数',
    'POTIM': 'MD 时间步长(fs)',
    'MDALGO': 'NVT:2=Nose-Hoover 恒温器 / NVE:1=Andersen(碰撞概率置0)',
    'SMASS': 'Nose-Hoover 热浴质量(0=用 VASP 默认 Nose 频率)',
    'ANDERSEN_PROB': 'Andersen 碰撞概率;0.0 → 无随机碰撞,退化为 NVE 微正则系综',
    'TEBEG': '初始/目标温度(K;NVE 下仅用于生成初速)',
    'TEEND': '终止温度(K;等温 MD 取与 TEBEG 同值,升/降温取不同值)',
    'ISYM': 'MD 轨迹破缺对称性,ISYM=0 必需(否则对称约束会污染动力学)',
    'NELMIN': '每 MD 步最少电子自洽步数,保证力平滑/能量守恒',
    'EDIFFG': 'MD 无离子弛豫收敛判据,剥离',
    'ISIF': '固定胞 NVT/NVE 无需应力/晶胞优化,剥离',
    'LANGEVIN_GAMMA': '当前派生的是 Nose-Hoover/Andersen，不保留旧 Langevin 摩擦参数',
    'LANGEVIN_GAMMA_L': '固定胞作业不保留旧 Langevin 晶格摩擦参数',
    'PMASS': '固定胞 NVT/NVE 不使用旧 Parrinello-Rahman 晶格质量',
    'ENCUT': 'AIMD 截断能(显式传 encut 时才改)',
}


def _read_text(path: str):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _structure_source(src_dir: str):
    """取弛豫末构型 → (来源名, 文本)。优先**非空** CONTCAR(避免 VASP 空 CONTCAR 陷阱),
    否则退 POSCAR;都无 → (None, None)。"""
    contcar = _read_text(os.path.join(src_dir, 'CONTCAR'))
    if contcar is not None and contcar.strip():
        return 'CONTCAR', contcar
    poscar = _read_text(os.path.join(src_dir, 'POSCAR'))
    if poscar is not None and poscar.strip():
        return 'POSCAR', poscar
    return None, None


def _fmt(x) -> str:
    """数值 → 简洁字符串(整数去小数尾:300.0→'300'、1.0→'1'、0.5→'0.5')。"""
    return f'{float(x):g}'


def _split_comment(line: str):
    """拆行为 (代码段, 注释段)。注释符取首个 '#' 或 '!'(与 freq_builder 同口径)。"""
    idxs = [i for i in (line.find('#'), line.find('!')) if i != -1]
    if idxs:
        i = min(idxs)
        return line[:i], line[i:]
    return line, ''


# ── AIMD 目标键(据系综/参数展开) ──────────────────────────────────────────────
def _aimd_targets(ensemble, temp_k, temp_end_k, steps, potim_fs, encut):
    """据系综/参数产出 (target: OrderedDict[key->值串], ens_norm)。未知系综 → ValueError。

    NVT:Nose-Hoover(MDALGO=2 + SMASS=0 + TEBEG/TEEND);
    NVE:Andersen 碰撞概率置0(MDALGO=1 + ANDERSEN_PROB=0.0),等价关闭恒温器的微正则。
    encut 显式给定才写入 ENCUT(不自动下调,尊重源 INCAR)。
    """
    ens = str(ensemble).lower()
    if ens not in ENSEMBLES:
        raise ValueError(f"未知系综 ensemble={ensemble!r};可选:{' / '.join(ENSEMBLES)}。")

    def _finite(name, value, *, positive=False):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'{name} 必须是有限数值,收到 {value!r}') from None
        if not math.isfinite(number):
            raise ValueError(f'{name} 必须是有限数值,收到 {value!r}')
        if positive and number <= 0:
            raise ValueError(f'{name} 必须大于 0,收到 {value!r}')
        return number

    try:
        steps_float = float(steps)
        steps_int = int(steps_float)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'steps 必须是正整数,收到 {steps!r}') from None
    if (isinstance(steps, bool) or not math.isfinite(steps_float)
            or steps_int <= 0 or steps_float != steps_int):
        raise ValueError(f'steps 必须是正整数,收到 {steps!r}')
    temp = _finite('temp_k', temp_k)
    if temp < 0:
        raise ValueError(f'temp_k 不能为负,收到 {temp_k!r}')
    temp_end = temp if temp_end_k is None else _finite('temp_end_k', temp_end_k)
    if temp_end < 0:
        raise ValueError(f'temp_end_k 不能为负,收到 {temp_end_k!r}')
    potim = _finite('potim_fs', potim_fs, positive=True)
    cutoff = None if encut is None else _finite('encut', encut, positive=True)

    t: OrderedDict = OrderedDict()
    t['IBRION'] = '0'
    t['NSW'] = str(steps_int)
    t['POTIM'] = _fmt(potim)
    t['ISYM'] = '0'
    t['NELMIN'] = '4'
    t['TEBEG'] = _fmt(temp)
    if ens == 'nvt':
        t['MDALGO'] = '2'
        t['SMASS'] = '0'
        t['TEEND'] = _fmt(temp_end)
    else:  # nve:Andersen 碰撞概率0 → 微正则
        t['MDALGO'] = '1'
        t['ANDERSEN_PROB'] = '0.0'
    if cutoff is not None:
        t['ENCUT'] = str(int(cutoff))
    return t, ens


def _aimd_incar_banner(changes: list) -> str:
    """把 changes 渲染为 INCAR 头部注释块(逐条注明,输出自带 changes 清单)。"""
    out = ['# === vcstudio AIMD 作业 INCAR(派生自弛豫 INCAR;只改 MD 必需项) ===']
    for c in changes:
        act, key = c['action'], c['key']
        if act == 'add':
            out.append(f"#   新增 {key} = {c['new']}  ({c['reason']})")
        elif act == 'replace':
            out.append(f"#   {key}: {c['old']} -> {c['new']}  ({c['reason']})")
        elif act == 'strip':
            out.append(f"#   剥离 {key}(原 {c['old']}):{c['reason']}")
    out.append('# 电子学参数(ENCUT/GGA/ISPIN/MAGMOM/IVDW/LDAU*)原样保留(除非显式传 encut)。')
    return '\n'.join(out) + '\n'


def _derive_aimd_incar(base_incar_text: str, target: OrderedDict):
    """源 INCAR 文本 + 目标键 → (AIMD INCAR 文本, changes 列表)。

    只改必须改的:替换/新增 target(IBRION/NSW/POTIM/MDALGO/... ),剥离 _AIMD_STRIP;其余键
    (含全部电子学参数)逐字保留,顺序不动。changes 每项 {'key','action','old','new','reason'}。
    子句级处理 ';' 多赋值,保留行内注释(与 _derive_freq_incar 同口径)。
    """
    parsed = parse_incar(base_incar_text)
    # target 之外的旧恒温器键不能残留。否则例如 NVT→NVE 派生仍带 SMASS/TEEND，
    # 或 Andersen→Nose 仍带 ANDERSEN_PROB，INCAR 表面显示新系综却混入旧控制参数。
    strip_keys = set(_AIMD_STRIP)
    if str(target.get('MDALGO')) == '1':
        strip_keys.update(('SMASS', 'TEEND'))
    else:
        strip_keys.add('ANDERSEN_PROB')
    changes: list = []
    handled: set = set()
    new_lines: list = []

    for line in base_incar_text.splitlines():
        code, comment = _split_comment(line)
        if '=' not in code:
            new_lines.append(line)          # 纯注释/空行/SYSTEM 头等原样
            continue
        kept = []
        for clause in code.split(';'):
            if '=' not in clause:
                if clause.strip():
                    kept.append(clause.strip())
                continue
            key = clause.split('=', 1)[0].strip().upper()
            if key in strip_keys:
                changes.append({'key': key, 'action': 'strip', 'old': parsed.get(key),
                                'new': None, 'reason': _AIMD_REASON.get(key, '')})
                continue                    # 丢弃该子句
            if key in target:
                new = target[key]
                changes.append({'key': key, 'action': 'replace', 'old': parsed.get(key),
                                'new': new, 'reason': _AIMD_REASON.get(key, '')})
                kept.append(f'{key} = {new}')
                handled.add(key)
                continue
            kept.append(clause.strip())     # 非目标键逐字保留
        if kept:
            merged = ' ; '.join(kept)
            new_lines.append(merged + (('  ' + comment) if comment else ''))
        # 整行子句全被剥离 → 连同注释丢弃

    # 补齐源 INCAR 里缺席但 MD 必须的键(按 target 顺序追加)
    for key, val in target.items():
        if key not in handled:
            changes.append({'key': key, 'action': 'add', 'old': None, 'new': val,
                            'reason': _AIMD_REASON.get(key, '')})
            new_lines.append(f'{key} = {val}')

    banner = _aimd_incar_banner(changes)
    return banner + '\n'.join(new_lines) + '\n', changes


def build_aimd_incar(base_incar_text: str, *, ensemble='nvt', temp_k=300.0,
                     temp_end_k=None, steps=10000, potim_fs=1.0, encut=None) -> str:
    """源 INCAR 文本 → AIMD INCAR 文本(含改动注释块)。结构化 changes 见 build_aimd_job。"""
    target, _ens = _aimd_targets(ensemble, temp_k, temp_end_k, steps, potim_fs, encut)
    return _derive_aimd_incar(base_incar_text, target)[0]


# ── 一键派生 AIMD 作业目录 ─────────────────────────────────────────────────────
def _save_aimd_manifest(out_dir, src_dir, poscar_text, source_name, changes, warnings,
                        ens, temp_k, temp_end_k, steps, potim_fs):
    """写 job.yaml:task_type='aimd',记 parent_job / 系综·温度·步长 / incar_changes 溯源。"""
    syms, _counts = parse_poscar_species(poscar_text)
    system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_dir).name
    parent = str(Path(src_dir).resolve())
    inputs = {
        'engine': 'vasp',
        'parent_job': parent,
        'derived_from': source_name,                 # CONTCAR / POSCAR
        'ensemble': ens,
        'temp_k': float(temp_k),
        'temp_end_k': float(temp_end_k) if temp_end_k is not None else float(temp_k),
        'steps': int(steps),
        'potim_fs': float(potim_fs),
        'incar_changes': changes,
        'elements': list(syms),
    }
    from vcstudio.generate.method_recipe import builder_recipe
    inputs['method_recipe'] = builder_recipe(
        builder='vcstudio.generate.aimd_builder/v1', task_type='aimd',
        calc_type='aimd', validate=True,
        completions={'incar_changes': changes}, kpoints_source='gamma-1x1x1',
        extra={
            'ensemble': ens, 'temp_k': float(temp_k),
            'temp_end_k': (float(temp_end_k) if temp_end_k is not None
                           else float(temp_k)),
            'steps': int(steps), 'potim_fs': float(potim_fs),
        })
    m = manifest_mod.new_manifest(
        job_id=f'{Path(out_dir).name}-aimd', system=system, task_type='aimd',
        calc_type='aimd', inputs=inputs, warnings=warnings)
    m['parent_job'] = parent                         # 顶层冗余一份,便于快速溯源
    from vcstudio.shared.scientific_inputs import record_input_closure
    record_input_closure(out_dir, m)
    manifest_mod.save_manifest(out_dir, m)
    return m


def build_aimd_job(src_dir, out_dir, *, ensemble='nvt', temp_k=300.0, temp_end_k=None,
                   steps=10000, potim_fs=1.0, encut=None) -> dict:
    """从完成弛豫的作业目录一键派生 AIMD 作业目录(论文第4章口径:NVT 300K 10ps 1fs)。

    读 src_dir 的 CONTCAR(缺则 POSCAR)+ INCAR + POTCAR,生成:
    CONTCAR→POSCAR(原样透传,保留既有 Selective dynamics 约束)+ 派生 MD INCAR(见
    build_aimd_incar)+ KPOINTS(Γ 点单点,AIMD 惯例)+ POTCAR 原样;写 job.yaml
    (task_type='aimd',记 parent_job 与系综/温度/步长/incar_changes 溯源)。

    参数:
        ensemble:'nvt'(Nose-Hoover)/ 'nve'(Andersen 碰撞概率0,微正则)。
        temp_k / temp_end_k:起/止温度(K);temp_end_k=None → 等温(取 temp_k)。
        steps:NSW MD 步数(默认 10000,即 10 ps @ 1 fs)。potim_fs:步长(默认 1 fs)。
        encut:显式给定才写 ENCUT(不自动下调;不给则继承源 INCAR 并 warning 提示 350eV 口径)。

    Returns: ``{'ok','job_dir','changes','warnings','error'}``。
    src/INCAR 缺失或系综非法 → ``ok=False`` + 中文 error(不抛,交调用方降级)。
    """
    src_dir = str(src_dir)
    # 1) 系综/参数校验(非法立即结构化返回)
    try:
        target, ens = _aimd_targets(ensemble, temp_k, temp_end_k, steps, potim_fs, encut)
    except ValueError as e:
        return {'ok': False, 'job_dir': None, 'changes': [], 'warnings': [], 'error': str(e)}

    # 2) 取初始构型 + 源 INCAR
    source_name, poscar_text = _structure_source(src_dir)
    if poscar_text is None:
        return {'ok': False, 'job_dir': None, 'changes': [], 'warnings': [],
                'error': f'源目录缺 CONTCAR/POSCAR(或均为空),无法派生 AIMD 作业:{src_dir}'}
    base_incar = _read_text(os.path.join(src_dir, 'INCAR'))
    if base_incar is None:
        return {'ok': False, 'job_dir': None, 'changes': [], 'warnings': [],
                'error': f'源目录缺 INCAR,无法派生 AIMD 作业:{src_dir}'}

    warnings: list = []
    if source_name == 'POSCAR':
        warnings.append('源目录 CONTCAR 缺失/为空,已退回 POSCAR 作初始构型'
                        '(建议用弛豫末态 CONTCAR 起 AIMD)。')

    aimd_incar, changes = _derive_aimd_incar(base_incar, target)
    if encut is None:
        warnings.append('论文口径 AIMD 常用 350 eV;当前继承源 INCAR 的 ENCUT 未自动下调,'
                        '如需降算力可显式传 encut(如 encut=350)。')
    if ens == 'nve':
        warnings.append('NVE 系综:以 MDALGO=1 + ANDERSEN_PROB=0.0(碰撞概率0)实现微正则'
                        '(等价关闭恒温器),初速由 TEBEG 温度生成。')

    # 3) 落盘四件套 + manifest
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
        f.write(poscar_text)
    with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(aimd_incar)
    with open(os.path.join(out_dir, 'KPOINTS'), 'w', encoding='utf-8') as f:
        f.write(kpoints_str([1, 1, 1]))
    warnings.append('AIMD 按惯例用 Γ 点单点(1×1×1)KPOINTS(大超胞下 Γ 足够);'
                    '若体系较小请自行加密 k 点并做收敛测试。')

    src_potcar = os.path.join(src_dir, 'POTCAR')
    if os.path.isfile(src_potcar):
        shutil.copyfile(src_potcar, os.path.join(out_dir, 'POTCAR'))
    else:
        warnings.append('源目录缺 POTCAR,未复制;提交前须补齐与源计算同一套赝势。')

    _save_aimd_manifest(out_dir, src_dir, poscar_text, source_name, changes, warnings,
                        ens, temp_k, temp_end_k, steps, potim_fs)
    return {'ok': True, 'job_dir': str(out_dir), 'changes': changes,
            'warnings': warnings, 'error': None}


# ── OSZICAR 的 MD 能量-时间解析(纯函数) ────────────────────────────────────────
# MD 行形如:「   1 T=  300. E= -.11453958E+03 F= -.11552627E+03 E0= ... EK= ... 」
# 抓 步号 / T=温度 / E=总能(含动能);电子自洽子行(DAV/RMM 等)不含 T=/E= 组合,不匹配。
# 数值容忍 Fortran 写法:'300.'(尾点无小数)、'-.1145E+03'(点前无整数)、常规小数/科学计数。
_NUM = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[EeDd][-+]?\d+)?'
_OSZ_MD_RE = re.compile(rf'^\s*(\d+)\s+T=\s*({_NUM})\s+E=\s*({_NUM})')


def parse_aimd_energy(oszicar_text, *, potim_fs: float = 1.0) -> dict:
    """解析 OSZICAR 的 MD 步行 → ``{'steps':[{'t_fs','e_tot','temp_k'}, ...], 'n'}``。

    每步取:步号(×potim_fs → 时间 t_fs)、T=温度(K)、E=总能(eV,含动能)。供能量-时间/
    温度-时间曲线(出图接线后续)。potim_fs 默认 1.0(论文口径),即 t_fs=步号;传入实际步长
    可换算真实 fs。非 MD 行(电子自洽子迭代/表头)自动跳过。
    """
    steps: list = []
    for line in str(oszicar_text).splitlines():
        m = _OSZ_MD_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        steps.append({'t_fs': n * float(potim_fs),
                      'e_tot': float(m.group(3)),
                      'temp_k': float(m.group(2))})
    return {'steps': steps, 'n': len(steps)}
