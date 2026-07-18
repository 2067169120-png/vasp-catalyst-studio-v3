"""分子属性计算(纯 python,零外部依赖)——化学式 / 电子数 / 质量 / 多重度初猜。

面向分子建模链的"属性面板":给定 per-atom 元素列表(+ 可选电荷),算出送 DFT 前
用户想先扫一眼的量——Hill 序化学式、总电子数(定 NELECT/校验)、分子量、以及**仅供
初猜**的自旋多重度(电子奇偶推断,绝不拍板:O2/双自由基等偶电子体系实际可为三重态,
须自旋极化核验)。

内置前 86 号元素(H–Rn)的标准原子量(IUPAC 常用值,amu)与原子序;未登记元素在
需要电子数/质量时显式报中文错误(不静默给 0)。formula 对任意符号串宽容(仅计数排序)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

# 前 86 号元素(H–Rn),按原子序排列:(符号, 标准原子量 amu)。
# 原子序 = 本表下标 + 1;质量取 IUPAC 常用标准原子量(放射性元素取常见同位素质量数)。
_ELEMENTS: tuple[tuple[str, float], ...] = (
    ('H', 1.008), ('He', 4.0026),
    ('Li', 6.94), ('Be', 9.0122), ('B', 10.81), ('C', 12.011),
    ('N', 14.007), ('O', 15.999), ('F', 18.998), ('Ne', 20.180),
    ('Na', 22.990), ('Mg', 24.305), ('Al', 26.982), ('Si', 28.085),
    ('P', 30.974), ('S', 32.06), ('Cl', 35.45), ('Ar', 39.948),
    ('K', 39.098), ('Ca', 40.078),
    ('Sc', 44.956), ('Ti', 47.867), ('V', 50.942), ('Cr', 51.996),
    ('Mn', 54.938), ('Fe', 55.845), ('Co', 58.933), ('Ni', 58.693),
    ('Cu', 63.546), ('Zn', 65.38),
    ('Ga', 69.723), ('Ge', 72.630), ('As', 74.922), ('Se', 78.971),
    ('Br', 79.904), ('Kr', 83.798), ('Rb', 85.468), ('Sr', 87.62),
    ('Y', 88.906), ('Zr', 91.224),
    ('Nb', 92.906), ('Mo', 95.95), ('Tc', 98.0), ('Ru', 101.07),
    ('Rh', 102.91), ('Pd', 106.42), ('Ag', 107.87), ('Cd', 112.41),
    ('In', 114.82), ('Sn', 118.71),
    ('Sb', 121.76), ('Te', 127.60), ('I', 126.90), ('Xe', 131.29),
    ('Cs', 132.91), ('Ba', 137.33), ('La', 138.91), ('Ce', 140.12),
    ('Pr', 140.91), ('Nd', 144.24),
    ('Pm', 145.0), ('Sm', 150.36), ('Eu', 151.96), ('Gd', 157.25),
    ('Tb', 158.93), ('Dy', 162.50), ('Ho', 164.93), ('Er', 167.26),
    ('Tm', 168.93), ('Yb', 173.05),
    ('Lu', 174.97), ('Hf', 178.49), ('Ta', 180.95), ('W', 183.84),
    ('Re', 186.21), ('Os', 190.23), ('Ir', 192.22), ('Pt', 195.08),
    ('Au', 196.97), ('Hg', 200.59),
    ('Tl', 204.38), ('Pb', 207.2), ('Bi', 208.98), ('Po', 209.0),
    ('At', 210.0), ('Rn', 222.0),
)

MAX_Z = len(_ELEMENTS)                                          # 86
_Z_BY_SYMBOL: dict[str, int] = {sym: i + 1 for i, (sym, _m) in enumerate(_ELEMENTS)}
_MASS_BY_SYMBOL: dict[str, float] = {sym: m for sym, m in _ELEMENTS}


def _norm_symbol(sym: str) -> str:
    """规整元素符号大小写:首字母大写、余字母小写(如 'FE'/'fe' → 'Fe')。"""
    s = str(sym).strip()
    if not s:
        return s
    return s[0].upper() + s[1:].lower()


def atomic_number(symbol: str) -> int:
    """元素符号 → 原子序(1–86)。未登记 → ValueError(不静默给 0)。"""
    z = _Z_BY_SYMBOL.get(_norm_symbol(symbol))
    if z is None:
        raise ValueError(f'未登记元素 {symbol!r}(内置仅前 {MAX_Z} 号 H–Rn)')
    return z


def element_symbol(z: int) -> str:
    """原子序(1–86) → 元素符号。越界 → ValueError。"""
    if not isinstance(z, int) or z < 1 or z > MAX_Z:
        raise ValueError(f'原子序 {z!r} 越界(内置支持 1–{MAX_Z})')
    return _ELEMENTS[z - 1][0]


def element_mass(symbol: str) -> float:
    """元素符号 → 标准原子量(amu)。未登记 → ValueError。"""
    m = _MASS_BY_SYMBOL.get(_norm_symbol(symbol))
    if m is None:
        raise ValueError(f'未登记元素 {symbol!r}(内置仅前 {MAX_Z} 号 H–Rn)')
    return m


def formula(elements) -> str:
    """per-atom 元素列表 → Hill 序化学式字符串。

    Hill 规则:含碳时 C 在前、H 次之,余元素按字母序;不含碳时全部按字母序(H 也参与
    字母序)。计数为 1 时省略数字(如 CH4 而非 C1H4)。空列表 → 空串。
    对任意符号串宽容(不校验是否登记),只计数与排序。
    """
    counts: dict[str, int] = {}
    for el in elements:
        s = _norm_symbol(el)
        counts[s] = counts.get(s, 0) + 1

    def _fmt(sym: str) -> str:
        n = counts[sym]
        return sym if n == 1 else f'{sym}{n}'

    parts: list[str] = []
    if 'C' in counts:
        parts.append(_fmt('C'))
        if 'H' in counts:
            parts.append(_fmt('H'))
        for sym in sorted(k for k in counts if k not in ('C', 'H')):
            parts.append(_fmt(sym))
    else:
        for sym in sorted(counts):
            parts.append(_fmt(sym))
    return ''.join(parts)


def n_electrons(elements, charge: int = 0) -> int:
    """总电子数 = Σ原子序 − 电荷(正电荷=失电子)。含未登记元素 → ValueError。"""
    total = sum(atomic_number(el) for el in elements)
    return total - int(charge)


def molecular_mass(elements) -> float:
    """分子量(amu)= Σ标准原子量。含未登记元素 → ValueError。"""
    return sum(element_mass(el) for el in elements)


_MULT_NOTE = ('仅按总电子数奇偶初猜(偶→单重态 1,奇→双重态 2);O2、双自由基等偶电子'
              '体系实际可为三重态,须以自旋极化(ISPIN=2)核验,非终值。')


def mol_summary(elements, coords=None, charge: int = 0) -> dict:
    """分子属性汇总。

    返回 ``{'formula','n_atoms','n_electrons','mass_amu','charge',
    'suggested_multiplicity','multiplicity_note'}``:
        - suggested_multiplicity:电子数为偶 → 1(单重态),奇 → 2(双重态);**仅初猜**。
        - coords 仅作接口对称保留(属性由元素与电荷确定,不用坐标);含未登记元素 → ValueError。
    """
    ne = n_electrons(elements, charge)
    mult = 1 if ne % 2 == 0 else 2
    return {
        'formula': formula(elements),
        'n_atoms': len(list(elements)),
        'n_electrons': ne,
        'mass_amu': round(molecular_mass(elements), 4),
        'charge': int(charge),
        'suggested_multiplicity': mult,
        'multiplicity_note': _MULT_NOTE,
    }
