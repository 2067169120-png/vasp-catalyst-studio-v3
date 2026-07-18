"""DFT+U 值库(Dudarev 口径):常用过渡金属/镧系 U 值注册表 + 建议 + LDAU 键组装。

DFT+U 对含局域 d/f 电子的过渡金属氧化物/氮化物等强关联体系,修正 GGA 的自相互作用误差
(能隙、磁矩、氧化还原能常需 +U)。本库收录文献/Materials Project 惯用起点值(Dudarev
方法,有效 U_eff = U − J,故只给一个数,J 取 0),按 POSCAR 元素序组装 LDAU 系列键。

**重要口径警示(每条都带):这些是"惯用起点值",不是普适真值。** +U 值依赖体系、氧化态、
赝势与目标性质(能隙 vs 生成能常需不同 U);发表前必须做 U 敏感性测试(扫 U 看目标量),
或改用无经验参数的方法(HSE/SCAN)交叉验证。本库只给起点、绝不宣称"正确 U"。

l 量子数约定:3d/4d/5d → l=2;4f(镧系)→ l=3;无 U 元素 → LDAUL=-1(VASP 语义)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

from collections import OrderedDict

_MP_SOURCE = 'Materials Project 惯用值(Dudarev,U_eff=U−J)'
_MP_NOTE = '惯用起点值,仅供起点;发表前须做 U 敏感性测试(扫 U 看目标量)或 HSE/SCAN 交叉验证。'

# 元素 → (U_eff eV, 轨道字母, l)。3d/4d/5d → l=2;4f → l=3。
# U 值取常见文献/MP 起点(氧化物语境);同元素不同氧化态/性质可能需不同 U,见 note。
U_LIBRARY = OrderedDict([
    ('Ti', {'u': 4.0, 'orbital': '3d', 'l': 2}),
    ('V',  {'u': 3.1, 'orbital': '3d', 'l': 2}),
    ('Cr', {'u': 3.5, 'orbital': '3d', 'l': 2}),
    ('Mn', {'u': 3.9, 'orbital': '3d', 'l': 2}),
    ('Fe', {'u': 4.0, 'orbital': '3d', 'l': 2}),
    ('Co', {'u': 3.3, 'orbital': '3d', 'l': 2}),
    ('Ni', {'u': 6.4, 'orbital': '3d', 'l': 2}),
    ('Cu', {'u': 4.0, 'orbital': '3d', 'l': 2}),
    ('Mo', {'u': 4.4, 'orbital': '4d', 'l': 2}),
    ('W',  {'u': 6.2, 'orbital': '5d', 'l': 2}),
    ('Ce', {'u': 5.0, 'orbital': '4f', 'l': 3}),
])


def suggest_u(elements) -> list:
    """按输入元素给出 U 建议 → ``[{'element','u','l','orbital','source','note'}, ...]``。

    只对库内登记的元素返回条目(保输入首现顺序、去重);未登记元素**不编造 U**,不进结果
    (调用方据此知道该元素无经验 U 值)。每条都带 source 与 note(敏感性测试警示)。
    """
    out, seen = [], set()
    for el in elements:
        if el in seen:
            continue
        seen.add(el)
        rec = U_LIBRARY.get(el)
        if rec is None:
            continue
        out.append({'element': el, 'u': rec['u'], 'l': rec['l'],
                    'orbital': rec['orbital'], 'source': _MP_SOURCE, 'note': _MP_NOTE})
    return out


def ldau_keys(suggestions, element_order) -> dict:
    """按 POSCAR 元素序组装 LDAU 系列 INCAR 键 → dict。

    Args:
        suggestions: suggest_u 的返回(或同结构 dict 列表);按 element 匹配。
        element_order: POSCAR 物种顺序(LDAUL/LDAUU/LDAUJ 必须与之逐位对齐,否则 U 加错元素)。

    Returns:
        ``{'LDAU': True, 'LDAUTYPE': 2, 'LDAUL': '2 -1 ...', 'LDAUU': '4.0 0.0 ...',
        'LDAUJ': '0.0 0.0 ...'}``。element_order 中未在 suggestions 的元素:LDAUL=-1、U=J=0
        (无 U)。suggestions 全空 → 仍返回全 -1/0 的键(显式声明"该体系不加 U",便于对照)。
    """
    by_el = {s['element']: s for s in (suggestions or [])}
    order = list(element_order)
    ldaul, ldauu, ldauj = [], [], []
    for el in order:
        s = by_el.get(el)
        if s is not None:
            ldaul.append(str(int(s['l'])))
            ldauu.append(f'{float(s["u"]):g}')
            ldauj.append('0')                  # Dudarev:J 并入 U_eff,显式 J=0
        else:
            ldaul.append('-1')                 # 无 U 元素:l=-1
            ldauu.append('0')
            ldauj.append('0')
    return {
        'LDAU': True, 'LDAUTYPE': 2,           # Dudarev（旋转不变简化,只用 U_eff）
        'LDAUL': ' '.join(ldaul),
        'LDAUU': ' '.join(ldauu),
        'LDAUJ': ' '.join(ldauj),
    }
