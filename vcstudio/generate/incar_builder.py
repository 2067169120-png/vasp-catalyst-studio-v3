"""INCAR 处理:文本解析(仅校验用)+ 校验补全 + 序列化。

**方法学不锁定**(与 E: 的统一 RPBE 版本本质不同):本模块**不生成** INCAR,只
解析用户 INCAR 以做校验判断,并产出【补全项】。剥离了 E: 的 RPBE/IVDW/ENCUT/ISPIN
强制锁定与 IMMUTABLE 守卫;保留 has_magnetic/build_magmom/incar_dict_to_str(拷贝)。

核心原则(决策1/3):**尊重用户 INCAR,绝不强改**。validate_and_complete_incar 只对
【缺失键】产补全项,值不合理(ENCUT 偏低/含磁却 ISPIN=1)只 warn 不改;从不 mutate 输入。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict
from typing import Optional

from vcstudio.generate import potcar

# ── 磁性元素 → 每原子初猜磁矩(μB) ──────────────────────────────────────────────
# 元素经验磁矩初猜(**初猜非终值**:仅给 VASP 起步磁态,自洽后应核对 OUTCAR 末尾 mag 并按需
# 覆盖)。数值取常见高自旋近似:3d 过渡金属按未配对 d 电子数量级,4d/5d 偏小,Ce/Gd 按 4f。
MAGNETIC_ELEMENTS = {
    'V': 3, 'Cr': 5, 'Mn': 5, 'Fe': 4, 'Co': 3, 'Ni': 2, 'Cu': 1,
    'Mo': 3, 'W': 2, 'Ce': 1, 'Gd': 7,
}
DEFAULT_MAGMOM = 5  # μB,磁性原子磁矩兜底(元素未登记经验磁矩时用)


def has_magnetic(elements) -> bool:
    """体系是否含磁性元素(MAGNETIC_ELEMENTS 键)。"""
    return any(el in MAGNETIC_ELEMENTS for el in elements)


def build_magmom(elements, counts,
                 magmom_overrides: Optional[dict] = None) -> Optional[str]:
    """生成 MAGMOM 串 ``'n1*m1 n2*m2 ...'``(按 elements 顺序)。

    磁性原子取 MAGNETIC_ELEMENTS 的元素经验磁矩(magmom_overrides 优先,登记缺失兜底
    DEFAULT_MAGMOM),非磁性原子 0。counts 缺失或与 elements 不等长 → None(调用方降级)。
    """
    if not counts or len(counts) != len(elements):
        return None
    overrides = dict(magmom_overrides or {})
    terms = []
    for el, c in zip(elements, counts):
        if el in overrides:
            moment = overrides[el]
        elif el in MAGNETIC_ELEMENTS:
            moment = MAGNETIC_ELEMENTS.get(el, DEFAULT_MAGMOM)   # 比矩;缺登记兜底
        else:
            moment = 0
        terms.append(f'{int(c)}*{moment:g}')
    return ' '.join(terms)


def incar_dict_to_str(incar: dict, system_name: str = '') -> str:
    """序列化 INCAR dict 为文件文本(拷贝自 E:)。

    - bool → ``.TRUE.`` / ``.FALSE.``
    - ``SYSTEM`` 行置首(system_name 或 incar['_system'])
    - 跳过 '_' 开头私有键
    """
    name = system_name or incar.get('_system', '')
    lines = []
    if name:
        lines.append(f'SYSTEM = {name}')
    for key, val in incar.items():
        if key.startswith('_') or (name and key.upper() == 'SYSTEM'):
            continue  # name 已写 SYSTEM 头 → 跳过 dict 里的 SYSTEM 键,避免重复行
        if isinstance(val, bool):
            val = '.TRUE.' if val else '.FALSE.'
        lines.append(f'{key} = {val}')
    return '\n'.join(lines) + '\n'


# ── INCAR 文本解析(净新,仅供校验判断,不决定输出) ──────────────────────────
_TRUE_TOKENS = {'.true.', '.t.', 't', 'true'}
_FALSE_TOKENS = {'.false.', '.f.', 'f', 'false'}
_INT_RE = re.compile(r'[+-]?\d+$')
_STAR_RE = re.compile(r'\d+\*')   # MAGMOM/LDAUU 星乘写法,如 16*0


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _scalar(tok: str):
    if _INT_RE.match(tok):
        return int(tok)
    try:
        return float(tok)
    except ValueError:
        return tok            # 字符串(ALGO/LREAL/PREC 等)


def _parse_value(raw: str):
    raw = raw.strip()
    low = raw.lower()
    if low in _TRUE_TOKENS:
        return True
    if low in _FALSE_TOKENS:
        return False
    parts = raw.split()
    if len(parts) > 1:
        # 老式 INCAR 惯例:标量值后直接跟英文说明、无 #/! 注释符(VASP 只读首
        # token),如 'ENCUT = 400.0  cut-off energy (eV)'。首 token 是数字且次
        # token 不像值(非数字/非星乘)→ 取首 token;否则真·多值串整体保留
        # (MAGMOM/LDAUU/DIPOL 不拆)。
        if _is_number(parts[0]) and not (_is_number(parts[1]) or _STAR_RE.match(parts[1])):
            return _scalar(parts[0])
        return raw
    return _scalar(raw)


def parse_incar(text: str) -> "OrderedDict":
    """解析 INCAR 文本 → OrderedDict(key 统一大写)。仅用于校验判断。

    剥注释(首个 # 或 !);按 ; 切多赋值;bool/int/float/多值串/字符串 类型推断。
    """
    result: "OrderedDict" = OrderedDict()
    for line in text.splitlines():
        for c in ('#', '!'):
            idx = line.find(c)
            if idx != -1:
                line = line[:idx]
        line = line.strip()
        if not line:
            continue
        for clause in line.split(';'):
            clause = clause.strip()
            if not clause or '=' not in clause:
                continue
            key, val = clause.split('=', 1)
            key = key.strip().upper()
            if key:
                result[key] = _parse_value(val)
    return result


# ── 校验补全(净新,决策表 D1-D4;只产补全项,绝不改用户键) ────────────────────
def _as_int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        f = float(str(v).strip())
    except (ValueError, TypeError):
        return None
    return int(f) if f == int(f) else None  # '2.0'/2.0 → 2(识别浮点写法的 ISPIN);'1.5' → None


def _as_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def validate_and_complete_incar(incar_dict, elements, counts,
                                lib_root: str | None = None,
                                force_encut: int | None = None):
    """尊重用户 INCAR,只对【缺失键】产补全项;值不合理只 warn 不改。

    force_encut:项目级统一 ENCUT。吸附能项目里各成员(clean_slab / slab+ads /
    gas_ref)元素并集不同,若各自按自身元素补 ENCUT 会得到**不同**截断能,使
    ΔE=E(slab+ads)−E(slab)−E(ref) 大数相减被不同基组静默污染(缺口分析指出的
    主打功能静默错误)。调用方(create_project)按全项目元素并集算好统一值传入,
    仅在用户 INCAR **未显式给** ENCUT 时生效(用户显式值永远尊重)。

    Returns:
        (completions: OrderedDict, warnings: list[str])。completions 仅含新增键
        (缺失的 ENCUT / MAGMOM / ISPIN);输入 incar_dict 不被 mutate。
        库不可达 / 元素未登记 → potcar.PotcarError(绝不静默)。
    """
    completions: "OrderedDict" = OrderedDict()
    warnings: list = []
    upper = {str(k).upper(): v for k, v in incar_dict.items()}
    present = set(upper)

    _enmax_cache = {}

    def enmax_max():
        if 'v' not in _enmax_cache:
            _enmax_cache['v'] = potcar.max_enmax(elements, lib_root)
        return _enmax_cache['v']

    # D1 / D2:ENCUT
    if 'ENCUT' not in present:
        if force_encut is not None:
            # 项目级统一:所有成员用同一 ENCUT,保 ΔE 可比性
            mx = enmax_max()
            if force_encut < mx:
                warnings.append(
                    f'项目统一 ENCUT={force_encut} 低于本成员元素最大 ENMAX={mx:.1f};'
                    f'POTCAR 拼接阶段将报错,请提高项目 ENCUT 或在 INCAR 显式给出。')
            completions['ENCUT'] = int(force_encut)
            warnings.append(
                f'INCAR 未指定 ENCUT,已按**全项目元素并集**统一补为 {int(force_encut)} eV'
                f'(保吸附能 ΔE 各成员基组一致)。')
        else:
            mx = enmax_max()
            enc = int(math.ceil(1.3 * mx / 50.0) * 50)  # 无 400 下限:不偷渡 RPBE 时代偏置
            completions['ENCUT'] = enc
            warnings.append(
                f'INCAR 未指定 ENCUT,已按 1.3×max(ENMAX)={mx:.1f} 补为 {enc} eV;'
                f'如需自定义请在 INCAR 显式给出。')
    else:
        user_encut = _as_number(upper.get('ENCUT'))
        if user_encut is not None:
            mx = enmax_max()
            if mx > user_encut:
                warnings.append(
                    f'用户 ENCUT={user_encut:g} 低于元素最大 ENMAX={mx:.1f};'
                    f'VASP 精度不足,POTCAR 拼接阶段将报错。请自行提高 ENCUT。')

    # D3 / D3' / D4:磁性
    if has_magnetic(elements):
        mags = sorted(set(el for el in elements if el in MAGNETIC_ELEMENTS))
        if _as_int(upper.get('ISPIN')) == 1:
            # D4:用户显式关自旋 → 尊重,不补,只 warn
            warnings.append(
                f'体系含磁性元素 {mags},但用户 INCAR 设 ISPIN=1(非自旋极化);'
                f'若非有意,磁性态将丢失。保留用户设置。')
        elif 'MAGMOM' not in present:
            magmom = build_magmom(elements, counts, None)
            if magmom is not None:
                completions['MAGMOM'] = magmom
                if 'ISPIN' not in present:
                    completions['ISPIN'] = 2
                warnings.append(
                    f"体系含磁性元素 {mags} 但 INCAR 无 MAGMOM,已补 MAGMOM='{magmom}'"
                    f'(按元素经验磁矩初猜,非终值)并设 ISPIN=2;顺序须与 POSCAR '
                    f'物种一致,自洽后请核对 mag 并按需覆盖。')
            else:
                # D3':含磁但 counts 缺失/畸形 → 无法生成 MAGMOM,但仍补 ISPIN=2 保自旋极化
                # (否则 VASP 默认 ISPIN=1 跑成非磁,静默错磁态)。
                if 'ISPIN' not in present:
                    completions['ISPIN'] = 2
                warnings.append(
                    f'体系含磁性元素 {mags} 但 POSCAR 未提供可用 counts(VASP4/畸形),'
                    f'无法生成 MAGMOM;已设 ISPIN=2 保证自旋极化,VASP 将用默认磁矩'
                    f'(可能非最优),建议提供 counts 以写入正确 MAGMOM。')
        # else:用户已带 MAGMOM → 完全不动(不重复补)
    return completions, warnings
