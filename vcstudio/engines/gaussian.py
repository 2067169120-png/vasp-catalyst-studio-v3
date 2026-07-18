"""Gaussian Backend——分子 gjf 生成 + log 能量/终止/虚频解析。

**仅支持分子**(参考态 / 溶剂化小分子):Gaussian 默认孤立体系,无周期性;传入
周期性 CalcSpec 直接 ValueError,绝不静默把周期结构当分子算。

Gaussian 软件本体用户自备,本适配只出输入(.gjf)、解析输出(.log)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import glob
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
_SCF_DONE_RE = re.compile(r'SCF Done:\s*E\([^)]*\)\s*=\s*([-+]?\d+\.\d+(?:[Ee][-+]?\d+)?)')
_FREQ_RE = re.compile(r'Frequencies\s*--\s*(.+)')


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


def _render(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec(分子)→ (gjf 全文, warnings)。build_gjf/generate_inputs/preview 唯一渲染源。

    **附加输入区顺序(Gaussian 铁律,本函数的接线决定)**:分子坐标块之后必空一行,
    随后各附加段依次为 Gen/GenECP 基组段 → ECP(Pseudo=Read)段 → SCRF=Read 自定义
    介电段,段与段之间恒以单空行分隔,文件以空行收尾。周期性 CalcSpec → ValueError。
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
    job = _JOB_KW.get(spec.task, 'sp')

    # 色散:按裁决口径写作功能后缀 '-D3'(路线行)。不同 Gaussian 版本可能需
    # EmpiricalDispersion=GD3/GD3BJ 关键字,提示用户据版本调整。
    disp = ''
    if spec.dispersion:
        disp = f'-{spec.dispersion}'
        warnings.append(
            f'色散以路线行后缀 {disp} 表达;若您的 Gaussian 版本需 '
            f'EmpiricalDispersion=GD3/GD3BJ 关键字,请据版本改写。')

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
    route_parts = [f'#P {func}{disp}', str(basis_token), job]
    if scrf_kw:
        route_parts.append(scrf_kw)
    if has_ecp:
        route_parts.append('Pseudo=Read')
    route = ' '.join(route_parts)

    lines = _resource_lines(spec.extras, _seedname(title))
    lines += [route, '', title, '', f'{int(spec.charge)} {int(spec.multiplicity)}']
    for el, xyz in zip(elements, cart):
        lines.append(f'{el:<2s} {xyz[0]:16.8f} {xyz[1]:16.8f} {xyz[2]:16.8f}')

    # 附加输入区(顺序:基组段 → ECP 段 → SCRF 段;段间单空行,坐标块后必空行)
    for section in (basis_lines, ecp_lines, scrf_lines):
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


def _log_path(out_dir: str) -> str | None:
    """定位 Gaussian 输出:优先 *.log,否则 *.out。"""
    for pat in ('*.log', '*.out'):
        cands = sorted(glob.glob(os.path.join(out_dir, pat)))
        if cands:
            return cands[0]
    return None


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
        return {'files': [path], 'warnings': warnings}

    def parse_energy(self, out_dir: str) -> dict:
        """log → 'SCF Done' 末次(Hartree→eV)+ Normal termination 判定 + 虚频计数。"""
        path = _log_path(out_dir)
        if path is None:
            return {'energy_ev': None, 'converged': False, 'n_imaginary': None,
                    'error': f'缺 Gaussian 输出(目录 {out_dir} 无 *.log / *.out)'}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()

        hits = _SCF_DONE_RE.findall(text)
        energy = float(hits[-1]) * HARTREE_TO_EV if hits else None

        normal = 'Normal termination of Gaussian' in text
        error_term = 'Error termination' in text
        converged = bool(normal and not error_term)
        n_imag = _count_imaginary(text)

        error = None
        if energy is None:
            error = 'Gaussian 输出未见 "SCF Done"(计算可能未完成/发散)'
        elif error_term:
            error = 'Gaussian 出现 Error termination(计算异常终止)'
        return {'energy_ev': energy, 'converged': converged,
                'n_imaginary': n_imag, 'error': error}

    def check_inputs(self, out_dir: str) -> list:
        """.gjf 存在性 + 路线行 / 电荷多重度行 / 坐标行齐全检查。"""
        cands = sorted(glob.glob(os.path.join(out_dir, '*.gjf')))
        if not cands:
            return ['缺 .gjf(Gaussian 输入不存在)。']
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
        return issues
