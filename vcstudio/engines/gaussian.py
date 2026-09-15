"""Gaussian Backend——分子 gjf 生成 + log 能量/终止/虚频解析。

**仅支持分子**(参考态 / 溶剂化小分子):Gaussian 默认孤立体系,无周期性;传入
周期性 CalcSpec 直接 ValueError,绝不静默把周期结构当分子算。

Gaussian 软件本体用户自备,本适配只出输入(.gjf)、解析输出(.log)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import glob
import math
import os
import re

from vcstudio.engines.calcspec import (
    HARTREE_TO_EV, CalcSpec, EngineBackend, parse_structure,
)

# 泛函 → Gaussian 关键字(未登记走原名 + warning)。
_FUNC_MAP = {
    'PBE': 'PBEPBE', 'PBE0': 'PBE1PBE', 'B3LYP': 'B3LYP', 'BLYP': 'BLYP',
    'TPSS': 'TPSSTPSS', 'M062X': 'M062X', 'M06': 'M06', 'WB97XD': 'wB97XD',
    'CAM-B3LYP': 'CAM-B3LYP',
}
_JOB_KW = {'relax': 'opt', 'static': 'sp', 'freq': 'freq'}
_DEF_BASIS = 'def2-SVP'
_DISPERSION_KW = {
    'D3': 'EmpiricalDispersion=GD3',
    'GD3': 'EmpiricalDispersion=GD3',
    'D3(BJ)': 'EmpiricalDispersion=GD3BJ',
    'D3BJ': 'EmpiricalDispersion=GD3BJ',
    'GD3BJ': 'EmpiricalDispersion=GD3BJ',
}

# 已知 SCRF 溶剂模型(仅作拼写校验;非此集不拒绝,仅 warning 提示核对)。
_SCRF_MODELS = ('SMD', 'PCM', 'CPCM', 'IEFPCM')

# 周期表元素符号(1-86 号,按原子序;供 GUI 周期表组件渲染 + 每元素基组建议)。
_PT_SYMBOLS = (
    'H', 'He',
    'Li', 'Be', 'B', 'C', 'N', 'O', 'F', 'Ne',
    'Na', 'Mg', 'Al', 'Si', 'P', 'S', 'Cl', 'Ar',
    'K', 'Ca', 'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn',
    'Ga', 'Ge', 'As', 'Se', 'Br', 'Kr',
    'Rb', 'Sr', 'Y', 'Zr', 'Nb', 'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd',
    'In', 'Sn', 'Sb', 'Te', 'I', 'Xe',
    'Cs', 'Ba', 'La', 'Ce', 'Pr', 'Nd', 'Pm', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy',
    'Ho', 'Er', 'Tm', 'Yb', 'Lu', 'Hf', 'Ta', 'W', 'Re', 'Os', 'Ir', 'Pt',
    'Au', 'Hg', 'Tl', 'Pb', 'Bi', 'Po', 'At', 'Rn',
)

# 元素类别 → 常用基组建议(仅起点,非定论):过渡金属/镧系默认含赝势的 LANL2DZ/SDD,
# 主族默认全电子 6-31G(d)/def2-SVP。
_BASIS_SUGGEST = {
    'main_group': ['6-31G(d)', 'def2-SVP'],
    'transition_metal': ['LANL2DZ', 'SDD'],
    'lanthanide': ['SDD'],
}
_CATEGORY_LABEL = {
    'main_group': '主族元素',
    'transition_metal': '过渡金属',
    'lanthanide': '镧系',
}


def _pt_category(z: int) -> str:
    """原子序 → 类别(3-12 族过渡金属 / 57-71 镧系 / 其余主族)。"""
    if 57 <= z <= 71:
        return 'lanthanide'
    if (21 <= z <= 30) or (39 <= z <= 48) or (72 <= z <= 80):
        return 'transition_metal'
    return 'main_group'


def _build_periodic_table() -> dict:
    """构造 PERIODIC_TABLE_GROUPS:元素 1-86(z/symbol/category/basis_suggestion)+ 类别表 + 起点声明。"""
    elements = []
    for i, sym in enumerate(_PT_SYMBOLS):
        cat = _pt_category(i + 1)
        elements.append({
            'z': i + 1, 'symbol': sym, 'category': cat,
            'basis_suggestion': list(_BASIS_SUGGEST[cat]),
        })
    categories = [
        {'key': k, 'label': _CATEGORY_LABEL[k], 'basis_suggestion': list(v),
         'needs_ecp': k in ('transition_metal', 'lanthanide')}
        for k, v in _BASIS_SUGGEST.items()
    ]
    return {
        'note': ('基组建议仅供起点(不是定论):过渡金属/镧系默认 LANL2DZ/SDD(含赝势),'
                 '主族默认 6-31G(d)/def2-SVP;实际须按体系与目标精度自行收敛验证。'),
        'categories': categories,
        'elements': elements,
    }


# GUI 周期表组件数据源:元素 1-86 按类别分组 + 每元素基组建议(仅起点,须自行验证)。
PERIODIC_TABLE_GROUPS = _build_periodic_table()


def suggest_basis(element: str) -> list:
    """元素符号 → 常用基组建议列表(未登记 → 空;仅起点,不是定论)。"""
    for e in PERIODIC_TABLE_GROUPS['elements']:
        if e['symbol'] == element:
            return list(e['basis_suggestion'])
    return []

# 'SCF Done:  E(RPBE) =  -76.1234  A.U. ...' → 末次电子能(Hartree)。
_NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?'
_SCF_DONE_RE = re.compile(rf'SCF Done:\s*E\([^)]*\)\s*=\s*({_NUMBER})')
_FREQ_RE = re.compile(r'Frequencies\s*--\s*(.+)')
_CORRELATED_ENERGY_PATTERNS = (
    ('CCSD(T)', re.compile(rf'CCSD\(T\)\s*=\s*({_NUMBER})', re.I)),
    ('CCSD', re.compile(rf'E\(CORR\)\s*=\s*({_NUMBER})', re.I)),
    ('MP2', re.compile(rf'EUMP2\s*=\s*({_NUMBER})', re.I)),
)
_GAUSSIAN_FAILURE_MARKERS = (
    'Error termination', 'Convergence failure', 'Optimization stopped',
    'Number of steps exceeded', 'Erroneous write', 'galloc: could not allocate memory',
)


def _resource_lines(extras: dict, seed: str) -> list:
    """extras → Link0 资源行(%nprocshared / %mem / %chk)。

    chk 缺省 True(向后兼容:旧版恒写 %chk=seed.chk);extras['chk'] 为字符串则取其名,
    为 False/空 则不写 chk 行。nproc/mem 为 0 或缺省时不写(0 核/0 GB 无意义)。
    """
    lines = []
    nproc = extras.get('nproc')
    if nproc:
        lines.append(f'%nprocshared={int(nproc)}')
    mem = extras.get('mem_gb')
    if mem:
        memv = int(mem) if float(mem) == int(float(mem)) else mem
        lines.append(f'%mem={memv}GB')
    chk = extras.get('chk', True)
    if chk:
        name = os.path.splitext(os.path.basename(chk))[0] or seed \
            if isinstance(chk, str) else seed
        lines.append(f'%chk={name}.chk')
    return lines


def _solvent_route_and_tail(solvent, warnings: list) -> tuple[str, list]:
    """溶剂 dict → (SCRF 路线关键字, 尾部附加段行)。空 → ('', [])。

    - 内置溶剂名:SCRF=(MODEL,Solvent=Name)(无尾段)。
    - 自定义介电 eps(+可选 epsinf):SCRF=(MODEL,Solvent=Generic,Read) + 尾段 eps=/epsinf=。
    """
    if not solvent:
        return '', []
    model = str(solvent.get('model', 'pcm')).upper()
    if model not in _SCRF_MODELS:
        warnings.append(
            f'SCRF 溶剂模型 {model!r} 非常见值({"/".join(_SCRF_MODELS)}),请核对拼写。')
    eps = solvent.get('eps')
    if eps is not None:
        tail = [f'eps={eps}']
        if solvent.get('epsinf') is not None:
            tail.append(f'epsinf={solvent["epsinf"]}')
        warnings.append(
            '自定义介电常数走 Solvent=Generic,Read:请确认 eps/epsinf 与所选 SCRF 模型'
            '兼容(SMD 泛化通常还需完整溶剂描述符,自定介电建议用 PCM/CPCM)。')
        return f'SCRF=({model},Solvent=Generic,Read)', tail
    name = str(solvent.get('name', 'water'))
    gname = name[:1].upper() + name[1:]
    warnings.append(
        f'溶剂 {gname!r} 按 Gaussian 内置溶剂名写入 SCRF;请核对该名在您的 Gaussian '
        f'版本溶剂表中存在(拼写区分大小写)。')
    return f'SCRF=({model},Solvent={gname})', []


def _mixed_basis_sections(mixed: dict, unique_elements: list,
                          warnings: list) -> tuple[str, list, list, bool]:
    """混合基组 dict → (basis_token, 基组段行, ECP 段行, has_ecp)。

    元素顺序稳定(按结构首现序分组);per_element/ecp_elements 指定但不在结构中的元素
    → warning 并忽略。有 ecp → basis_token='GenECP'(否则 'Gen')。
    """
    default_b = mixed.get('default') or _DEF_BASIS
    per_el = dict(mixed.get('per_element') or {})
    ecp_els = list(mixed.get('ecp_elements') or [])
    present = set(unique_elements)
    for el in per_el:
        if el not in present:
            warnings.append(f'mixed_basis.per_element 元素 {el!r} 不在结构中,已忽略。')
    for el in ecp_els:
        if el not in present:
            warnings.append(f'mixed_basis.ecp_elements 元素 {el!r} 不在结构中,已忽略。')
    ecp_set = {el for el in ecp_els if el in present}
    ecp_present = [el for el in unique_elements if el in ecp_set]
    has_ecp = bool(ecp_present)

    def _group(elems, basis_of):
        """按基组值分组(组序 = 该基组首个元素的出现序;组内元素保持出现序)。"""
        order, table = [], {}
        for el in elems:
            b = basis_of(el)
            if b not in table:
                table[b] = []
                order.append(b)
            table[b].append(el)
        return [(b, table[b]) for b in order]

    basis_lines = []
    for b, els in _group(unique_elements, lambda e: per_el.get(e, default_b)):
        basis_lines += [f'{" ".join(els)} 0', str(b), '****']
    ecp_lines = []
    if has_ecp:
        for en, els in _group(ecp_present, lambda e: per_el.get(e, default_b)):
            ecp_lines += [f'{" ".join(els)} 0', str(en)]
    return ('GenECP' if has_ecp else 'Gen'), basis_lines, ecp_lines, has_ecp


# ── Gaussian 任务种类全家桶(extras['gaussian_task'] 覆盖 CalcSpec.task 映射) ──────
def _positive_int(extras: dict, key: str, default, label: str) -> int:
    """extras[key] → 正整数(缺省用 default);非正整数 → 中文 ValueError。"""
    v = extras.get(key, default)
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"extras[{key!r}] 需为正整数({label}),收到 {v!r}。")
    if iv <= 0:
        raise ValueError(f"extras[{key!r}] 需为正整数({label}),收到 {v!r}。")
    return iv


def _modredundant_section(extras: dict) -> list:
    """extras['modredundant'] → ModRedundant 段行列表(如 ['B 1 2 S 10 0.1'])。缺失 → 中文 ValueError。"""
    mr = extras.get('modredundant')
    if not mr:
        raise ValueError(
            "gaussian_task='scan' 需在 extras['modredundant'] 提供 ModRedundant 扫描定义"
            "(list[str],如 ['B 1 2 S 10 0.1']:键长 1-2,扫 10 步,每步 +0.1 Å)。")
    if isinstance(mr, str):
        mr = [mr]
    return [str(x) for x in mr]


# 任务 key → {route_fn(extras)->路线关键字, name_zh(GUI 下拉标签), note}。
# route_fn 只产路线关键字;scan 的 ModRedundant 附加段在 _render 里单独按顺序输出。
GAUSSIAN_TASKS = {
    'opt': {
        'route_fn': lambda e: 'opt',
        'name_zh': '结构优化', 'note': 'Opt:标准几何优化(等价 CalcSpec.task=relax)。',
    },
    'freq': {
        'route_fn': lambda e: 'freq',
        'name_zh': '频率分析', 'note': 'Freq:简谐频率/热化学(须在优化后的构型上做)。',
    },
    'opt_freq': {
        'route_fn': lambda e: 'Opt Freq',
        'name_zh': '优化+频率', 'note': 'Opt Freq:优化后连跑频率(一步拿到极小点+热校正)。',
    },
    'sp': {
        'route_fn': lambda e: 'sp',
        'name_zh': '单点能', 'note': 'SP:单点能(等价 CalcSpec.task=static)。',
    },
    'td': {
        'route_fn': lambda e: f'TD=(NStates={_positive_int(e, "td_nstates", 6, "TD 激发态数")})',
        'name_zh': '激发态 TD-DFT',
        'note': "TD=(NStates=N):含时 DFT 激发态,N 取 extras['td_nstates'](默认 6)。",
    },
    'irc': {
        'route_fn': lambda e: f'IRC=(CalcFC,MaxPoints={_positive_int(e, "irc_maxpoints", 20, "IRC 最大路径点数")})',
        'name_zh': '内禀反应坐标 IRC',
        'note': "IRC=(CalcFC,MaxPoints=N):从过渡态沿 IRC 下山,N 取 extras['irc_maxpoints'](默认 20)。",
    },
    'scan': {
        'route_fn': lambda e: 'Opt=ModRedundant',
        'name_zh': '刚性/柔性扫描',
        'note': "Opt=ModRedundant:冗余内坐标扫描,须给 extras['modredundant'](坐标块后附加段)。",
    },
    'nmr': {
        'route_fn': lambda e: 'NMR=GIAO',
        'name_zh': 'NMR 屏蔽', 'note': 'NMR=GIAO:GIAO 法磁屏蔽张量(化学位移)。',
    },
    'opt_ts': {
        'route_fn': lambda e: 'Opt=(TS,CalcFC,NoEigenTest) Freq',
        'name_zh': '过渡态优化',
        'note': ('Opt=(TS,CalcFC,NoEigenTest) Freq:过渡态优化并连跑 Freq 验证——'
                 'TS 须恰有 1 个虚频(反应坐标方向),Freq 用于确认单虚频。'),
    },
}


def _gaussian_task_route(gtask: str, extras: dict) -> tuple[str, list]:
    """gaussian_task → (路线关键字, ModRedundant 附加段行)。未知任务/参数非法 → 中文 ValueError。"""
    key = str(gtask).lower()
    if key not in GAUSSIAN_TASKS:
        raise ValueError(
            f'未知 gaussian_task {gtask!r};可选:{", ".join(GAUSSIAN_TASKS)}。')
    route = GAUSSIAN_TASKS[key]['route_fn'](extras)   # td/irc 参数非法在此抛
    modred = _modredundant_section(extras) if key == 'scan' else []
    return route, modred


def _render(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec(分子)→ (gjf 全文, warnings)。build_gjf/generate_inputs/preview 唯一渲染源。

    **附加输入区顺序(Gaussian 铁律,本函数的接线决定)**:分子坐标块之后必空一行,
    随后各附加段依次为 ModRedundant 扫描段(仅 scan 任务) → Gen/GenECP 基组段 →
    ECP(Pseudo=Read)段 → SCRF=Read 自定义介电段,段与段之间恒以单空行分隔,文件以
    空行收尾。任务路线由 extras['gaussian_task'] 决定(见 GAUSSIAN_TASKS),未给时退回
    CalcSpec.task 的 relax/static/freq 旧映射。周期性 CalcSpec → ValueError。
    """
    if spec.periodic:
        raise ValueError(
            'Gaussian 适配仅支持分子(参考态/溶剂化);收到周期性 CalcSpec'
            '(periodic=True),周期体系请用 VASP/CP2K/CASTEP。')
    warnings: list = []
    struct = parse_structure(spec.structure)
    elements, cart = struct['elements'], struct['cart']
    title = struct['comment'] or spec.extras.get('system') or 'vcstudio molecule'

    key = str(spec.functional).upper()
    func = _FUNC_MAP.get(key)
    if func is None:
        func = str(spec.functional)
        warnings.append(
            f'未登记 Gaussian 泛函关键字 {spec.functional!r},已按原名写入路线行,请核对。')

    basis = spec.extras.get('basis', _DEF_BASIS)
    # 任务路线:extras['gaussian_task'] 显式给定 → 走全家桶(可含多关键字/扫描附加段);
    # 未给 → 保持旧行为(CalcSpec.task 经 _JOB_KW 映射 relax/static/freq,逐字节不变)。
    gtask = spec.extras.get('gaussian_task')
    if gtask is None:
        job = _JOB_KW.get(spec.task, 'sp')
        modredundant_lines: list = []
    else:
        job, modredundant_lines = _gaussian_task_route(gtask, spec.extras)

    # Gaussian D3 is a route keyword, not a functional-name suffix.  Writing
    # ``PBEPBE-D3`` is accepted by neither many Gaussian releases nor all
    # functionals and can turn a scientifically intended D3 run into a parse
    # error.  Keep the mapping explicit and fail on unknown spellings.
    disp_kw = ''
    if spec.dispersion:
        dkey = str(spec.dispersion).upper().replace(' ', '')
        disp_kw = _DISPERSION_KW.get(dkey, '')
        if not disp_kw:
            raise ValueError(
                f'Gaussian 色散 {spec.dispersion!r} 未登记；可选 D3/GD3 或 D3(BJ)/GD3BJ，'
                '拒绝猜测路线关键字。')
        warnings.append(
            f'色散已显式写为 {disp_kw}；请确认所用 Gaussian 版本与该参数化兼容。')

    # 唯一元素(按结构首现序;供混合基组分组稳定)
    seen, unique_elements = set(), []
    for el in elements:
        if el not in seen:
            seen.add(el)
            unique_elements.append(el)

    # 混合基组 Gen/GenECP(附加段:基组段 + ECP 段)
    mixed = spec.extras.get('mixed_basis')
    if mixed:
        basis_token, basis_lines, ecp_lines, has_ecp = _mixed_basis_sections(
            mixed, unique_elements, warnings)
    else:
        basis_token, basis_lines, ecp_lines, has_ecp = basis, [], [], False

    # 溶剂 SCRF(路线关键字 + 自定介电尾段)
    scrf_kw, scrf_lines = _solvent_route_and_tail(spec.extras.get('solvent'), warnings)

    # 路线行:泛函(+色散后缀) 基组位 任务 [SCRF] [Pseudo=Read]
    route_parts = [f'#P {func}', str(basis_token), job]
    if disp_kw:
        route_parts.append(disp_kw)
    if scrf_kw:
        route_parts.append(scrf_kw)
    if has_ecp:
        route_parts.append('Pseudo=Read')
    route = ' '.join(route_parts)

    lines = _resource_lines(spec.extras, _seedname(title))
    lines += [route, '', title, '', f'{int(spec.charge)} {int(spec.multiplicity)}']
    for el, xyz in zip(elements, cart):
        lines.append(f'{el:<2s} {xyz[0]:16.8f} {xyz[1]:16.8f} {xyz[2]:16.8f}')

    # 附加输入区(顺序:ModRedundant 段 → 基组段 → ECP 段 → SCRF 段;段间单空行,坐标块后必空行)。
    # ModRedundant(仅 scan 任务)必须紧跟坐标块、排在基组/ECP/SCRF 之前(Gaussian 铁律)。
    for section in (modredundant_lines, basis_lines, ecp_lines, scrf_lines):
        if section:
            lines.append('')
            lines.extend(section)
    lines.append('')                                    # gjf 末尾必空行
    return '\n'.join(lines) + '\n', warnings


def build_gjf(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec(分子)→ Gaussian 输入文本(.gjf)+ warnings。周期性 → ValueError。

    与 preview/generate_inputs 共用同一 _render(spec) 渲染源(生成逻辑单一来源)。
    """
    return _render(spec)


def preview(spec: CalcSpec) -> str:
    """不落盘直接返回 gjf 全文(GUI 实时预览用);与 generate_inputs 完全同源(_render)。"""
    return _render(spec)[0]


def _seedname(title: str) -> str:
    """标题 → 安全 chk 名(取首 token,非字母数字转下划线)。"""
    tok = (title.split() or ['mol'])[0]
    return re.sub(r'[^A-Za-z0-9_.-]', '_', tok) or 'mol'


def _log_path(out_dir: str) -> tuple[str | None, str | None]:
    """定位 Gaussian 输出；多个看似有效的日志时拒绝按字母序猜旧结果。"""
    for pat in ('*.log', '*.out'):
        matching = []
        for path in sorted(glob.glob(os.path.join(out_dir, pat))):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                    probe = handle.read(256000)
            except OSError:
                continue
            if any(mark in probe for mark in (
                    'Entering Gaussian System', 'Gaussian, Inc.', 'SCF Done:',
                    'Normal termination of Gaussian', 'Error termination')):
                matching.append(path)
        if len(matching) == 1:
            return matching[0], None
        if len(matching) > 1:
            return None, ('发现多个 Gaussian 主输出，无法确定本轮日志：'
                          + '、'.join(os.path.basename(path) for path in matching))
    return None, None


def _count_imaginary(text: str):
    """统计虚频(Frequencies -- 行中的负值)。无频率行 → None。"""
    seen = False
    n_imag = 0
    for m in _FREQ_RE.finditer(text):
        seen = True
        for tok in m.group(1).split():
            try:
                if float(tok) < 0:
                    n_imag += 1
            except ValueError:
                continue
    return n_imag if seen else None


def _frequencies(text: str) -> list[float]:
    values = []
    for match in _FREQ_RE.finditer(text or ''):
        for token in match.group(1).split():
            try:
                value = float(token.replace('D', 'E').replace('d', 'e'))
            except ValueError:
                continue
            if math.isfinite(value):
                values.append(value)
    return values


def _last_electronic_energy(text: str) -> tuple[float | None, str | None]:
    """Return the last SCF/MP2/CC electronic-energy record in file order."""
    records = []
    for match in _SCF_DONE_RE.finditer(text or ''):
        records.append((match.start(), match.group(1), 'SCF Done'))
    for source, pattern in _CORRELATED_ENERGY_PATTERNS:
        for match in pattern.finditer(text or ''):
            records.append((match.start(), match.group(1), source))
    if not records:
        return None, None
    _position, token, source = max(records, key=lambda item: item[0])
    try:
        value = float(token.replace('D', 'E').replace('d', 'e'))
    except ValueError:
        return None, None
    return (value * HARTREE_TO_EV, source) if math.isfinite(value) else (None, None)


def _task_from_route(text: str) -> str | None:
    route_lines = []
    active = False
    for line in (text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            active = True
            route_lines.append(stripped)
        elif active and stripped:
            route_lines.append(stripped)
        elif active:
            break
    route = ' '.join(route_lines).lower()
    if not route:
        return None
    if 'opt=(' in route and 'ts' in route and 'freq' in route:
        return 'opt_ts'
    if 'opt' in route and 'freq' in route:
        return 'opt_freq'
    if 'opt=modredundant' in route:
        return 'scan'
    if 'irc' in route:
        return 'irc'
    if re.search(r'\bfreq\b', route):
        return 'freq'
    if re.search(r'\bopt\b', route):
        return 'opt'
    return 'static'


def _last_orientation(text: str) -> dict | None:
    """Extract the last complete Gaussian orientation table (Å)."""
    lines = (text or '').splitlines()
    last = None
    row_re = re.compile(
        rf'^\s*\d+\s+(\d+)\s+\d+\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*$')
    for start, line in enumerate(lines):
        if 'Standard orientation:' not in line and 'Input orientation:' not in line:
            continue
        separators = []
        for index in range(start + 1, min(len(lines), start + 12)):
            if re.match(r'^\s*-{10,}\s*$', lines[index]):
                separators.append(index)
                if len(separators) == 2:
                    break
        if len(separators) < 2:
            continue
        atoms = []
        for row in lines[separators[1] + 1:]:
            if re.match(r'^\s*-{10,}\s*$', row):
                break
            match = row_re.match(row)
            if not match:
                atoms = []
                break
            atomic_number = int(match.group(1))
            element = (_PT_SYMBOLS[atomic_number - 1]
                       if 1 <= atomic_number <= len(_PT_SYMBOLS) else f'Z{atomic_number}')
            xyz = [float(match.group(i).replace('D', 'E').replace('d', 'e'))
                   for i in (2, 3, 4)]
            atoms.append({'element': element, 'atomic_number': atomic_number,
                          'xyz_angstrom': xyz})
        if atoms:
            last = {'atoms': atoms, 'coordinate_system': 'cartesian_angstrom'}
    return last


def _thermochemistry(text: str) -> dict:
    result = {}
    patterns = {
        'zero_point_correction_ev': r'Zero-point correction=\s*(' + _NUMBER + r')',
        'thermal_energy_correction_ev': r'Thermal correction to Energy=\s*(' + _NUMBER + r')',
        'thermal_free_energy_correction_ev':
            r'Thermal correction to Gibbs Free Energy=\s*(' + _NUMBER + r')',
        'sum_electronic_thermal_free_energy_ev':
            r'Sum of electronic and thermal Free Energies=\s*(' + _NUMBER + r')',
    }
    for key, pattern in patterns.items():
        hits = re.findall(pattern, text or '', flags=re.I)
        if hits:
            result[key] = float(hits[-1].replace('D', 'E').replace('d', 'e')) * HARTREE_TO_EV
    return result


def parse_output_text(text: str, *, task: str | None = None) -> dict:
    """Parse Gaussian output and require task-level evidence, not just a footer."""
    task_key = str(task or '').strip().lower()
    task_key = {'relax': 'opt', 'static': 'static'}.get(task_key, task_key)
    if task_key not in set(GAUSSIAN_TASKS) | {'static'}:
        task_key = 'static'
    energy, source = _last_electronic_energy(text)
    frequencies = _frequencies(text)
    n_imag = sum(value < 0 for value in frequencies) if frequencies else None
    normal_at = text.rfind('Normal termination of Gaussian')
    error_at = max((text.rfind(marker) for marker in _GAUSSIAN_FAILURE_MARKERS), default=-1)
    normal = normal_at >= 0 and normal_at > error_at
    failures = [marker for marker in _GAUSSIAN_FAILURE_MARKERS
                if marker.lower() in text.lower()]
    # Failure text from an earlier Link is still relevant unless a later normal
    # termination belongs to a fully completed multi-Link job.  Error
    # termination is never recoverable inside the same output.
    failed = 'Error termination' in text or (bool(failures) and not normal)
    opt_done = ('Stationary point found.' in text
                or 'Optimization completed.' in text)
    if task_key in ('opt',):
        task_done = opt_done
    elif task_key == 'opt_freq':
        task_done = opt_done and bool(frequencies)
    elif task_key == 'opt_ts':
        task_done = opt_done and bool(frequencies) and n_imag == 1
    elif task_key == 'freq':
        task_done = bool(frequencies)
    elif task_key in ('scan', 'irc'):
        task_done = energy is not None
    else:
        task_done = energy is not None
    converged = bool(normal and task_done and energy is not None and not failed)
    error = None
    if failed:
        error = 'Gaussian 输出含失败标志：' + '、'.join(failures or ['Error termination'])
    elif energy is None:
        error = 'Gaussian 输出未见可用电子能（SCF Done/EUMP2/CCSD）'
    elif not normal:
        error = 'Gaussian 输出缺最终 Normal termination 页脚，拒绝把截断日志判为完成'
    elif not task_done:
        error = f'Gaussian {task_key} 正常退出但缺任务级完成证据（优化/频率/TS 虚频门）'
    result = {
        'energy_ev': energy, 'energy_source': source,
        'converged': converged, 'normal_termination': normal,
        'task_converged': bool(task_done), 'failed': failed,
        'failure_markers': failures, 'task': task_key,
        'frequencies_cm1': frequencies, 'n_imaginary': n_imag,
        'error': error,
    }
    structure = _last_orientation(text)
    if structure:
        result['final_structure'] = structure
        result['structure_source'] = 'last Standard/Input orientation'
    result.update(_thermochemistry(text))
    return result


class GaussianBackend(EngineBackend):
    """Gaussian 文件级适配(仅分子)。"""

    name = 'gaussian'

    def generate_inputs(self, spec: CalcSpec, out_dir: str) -> dict:
        os.makedirs(out_dir, exist_ok=True)
        text, warnings = build_gjf(spec)
        # 文件名与内部 %chk 同源(结构注释 → seedname),保持一致。
        title = parse_structure(spec.structure)['comment'] \
            or spec.extras.get('system') or 'input'
        path = os.path.join(out_dir, f'{_seedname(title)}.gjf')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        warnings.append('Gaussian 软件本体用户自备;本适配仅出 .gjf。')
        output_files = list(self.run_contract.result_files(
            [os.path.basename(path)], task=spec.extras.get('gaussian_task') or spec.task))
        chk_match = re.search(r'^%chk=(.+)$', text, flags=re.MULTILINE | re.IGNORECASE)
        if chk_match:
            chk_name = os.path.basename(chk_match.group(1).strip())
            output_files = [name for name in output_files if not name.lower().endswith('.chk')]
            output_files.append(chk_name)
        return {
            'files': [path], 'warnings': warnings, 'output_files': output_files,
            'restart': {'supported': self.run_contract.restart_supported,
                        'note': self.run_contract.restart_note},
        }

    def parse_energy(self, out_dir: str) -> dict:
        """log → 'SCF Done' 末次(Hartree→eV)+ Normal termination 判定 + 虚频计数。"""
        path, select_error = _log_path(out_dir)
        if path is None:
            return {'energy_ev': None, 'converged': False, 'n_imaginary': None,
                    'error': select_error or
                             f'缺 Gaussian 输出(目录 {out_dir} 无唯一可识别 *.log / *.out)'}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()

        parsed = parse_output_text(text, task=self.infer_task(out_dir))
        parsed['output_file'] = os.path.basename(path)
        return parsed

    def parse_output_text(self, text: str, *, task: str | None = None) -> dict:
        return parse_output_text(text, task=task)

    def infer_task(self, out_dir: str) -> str | None:
        cands = sorted(glob.glob(os.path.join(out_dir, '*.gjf'))
                       + glob.glob(os.path.join(out_dir, '*.com')))
        if len(cands) != 1:
            return None
        try:
            with open(cands[0], 'r', encoding='utf-8', errors='replace') as handle:
                return _task_from_route(handle.read())
        except OSError:
            return None

    def check_inputs(self, out_dir: str) -> list:
        """.gjf 存在性 + 路线行 / 电荷多重度行 / 坐标行齐全检查。"""
        cands = sorted(glob.glob(os.path.join(out_dir, '*.gjf'))
                       + glob.glob(os.path.join(out_dir, '*.com')))
        if not cands:
            return ['缺 .gjf/.com(Gaussian 输入不存在)。']
        if len(cands) > 1:
            return ['存在多个 Gaussian .gjf/.com，无法确定 {input}/{stem} 对应的主输入。']
        with open(cands[0], 'r', encoding='utf-8', errors='replace') as f:
            lines = [ln.rstrip('\n') for ln in f]
        issues: list = []
        if not any(ln.lstrip().startswith('#') for ln in lines):
            issues.append('.gjf 缺路线行(以 # 开头,含泛函/基组/任务)。')
        # 电荷多重度行:两个整数
        if not any(re.fullmatch(r'[+-]?\d+\s+\d+', ln.strip()) for ln in lines):
            issues.append('.gjf 缺电荷/多重度行(如 "0 1")。')
        if not any(re.match(r'^[A-Za-z]{1,2}(\s+[-+]?\d)', ln.strip()) for ln in lines):
            issues.append('.gjf 缺笛卡尔坐标行(元素 + xyz)。')
        for line in lines:
            match = re.match(r'^\s*%chk\s*=\s*(\S+)\s*$', line, re.I)
            if match:
                name = match.group(1)
                if os.path.basename(name) != name or '/' in name or '\\' in name:
                    issues.append(
                        '.gjf 的 %chk 必须是作业目录内单文件名；绝对/子目录路径无法由结果回收契约安全下载。')
        return issues
