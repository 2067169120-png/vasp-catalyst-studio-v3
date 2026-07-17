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

# 'SCF Done:  E(RPBE) =  -76.1234  A.U. ...' → 末次电子能(Hartree)。
_SCF_DONE_RE = re.compile(r'SCF Done:\s*E\([^)]*\)\s*=\s*([-+]?\d+\.\d+(?:[Ee][-+]?\d+)?)')
_FREQ_RE = re.compile(r'Frequencies\s*--\s*(.+)')


def build_gjf(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec(分子)→ Gaussian 输入文本(.gjf)+ warnings。周期性 → ValueError。"""
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

    route = f'#P {func}{disp} {basis} {job}'

    lines = [
        f'%chk={_seedname(title)}.chk',
        route,
        '',
        title,
        '',
        f'{int(spec.charge)} {int(spec.multiplicity)}',
    ]
    for el, xyz in zip(elements, cart):
        lines.append(f'{el:<2s} {xyz[0]:16.8f} {xyz[1]:16.8f} {xyz[2]:16.8f}')
    lines.append('')                                    # gjf 末尾必空行
    return '\n'.join(lines) + '\n', warnings


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
