"""POTCAR_VARIANT 扩表回归:S1 新增 28 元素(2026-07-02 对用户 potpaw_PBE.54 核对)。

只测纯表映射,不碰真实赝势库(读库行为由 read_enmax/build_potcar 的既有测试覆盖)。
"""
import pytest

from vcstudio.generate.potcar import POTCAR_VARIANT, PotcarError, variant
from vcstudio.generate.incar_builder import MAGNETIC_ELEMENTS


# 催化刚需 + 各族代表:元素 → 经核对的变体
NEW_ENTRIES = {
    'H': 'H', 'O': 'O',
    'F': 'F', 'Cl': 'Cl', 'Br': 'Br', 'I': 'I',
    'Na': 'Na_pv', 'K': 'K_sv', 'Rb': 'Rb_sv', 'Cs': 'Cs_sv',
    'Be': 'Be', 'Mg': 'Mg', 'Ca': 'Ca_sv', 'Sr': 'Sr_sv', 'Ba': 'Ba_sv',
    'Al': 'Al', 'Si': 'Si', 'Ga': 'Ga_d', 'Ge': 'Ge_d', 'As': 'As',
    'Se': 'Se', 'Sn': 'Sn_d', 'Sb': 'Sb', 'Te': 'Te', 'Pb': 'Pb_d', 'Bi': 'Bi_d',
    'La': 'La', 'Ce': 'Ce',
}


def test_new_elements_registered_with_verified_variants():
    for el, var in NEW_ENTRIES.items():
        assert variant(el) == var, f'{el} 应映射到 {var}'


def test_legacy_entries_untouched():
    # 抽查旧表关键条目未被扩表破坏
    assert variant('Fe') == 'Fe'
    assert variant('V') == 'V_sv'
    assert variant('Ta') == 'Ta_pv'
    assert variant('Li') == 'Li'


def test_table_size_covers_62_elements():
    assert len(POTCAR_VARIANT) >= 62


def test_unregistered_element_still_raises():
    # 扩表不改变"未登记显式报错"的底线
    with pytest.raises(PotcarError, match='未在 POTCAR_VARIANT 登记'):
        variant('Xx')


def test_magnetic_elements_is_per_element_moment_dict():
    # 变更(科学防错包 任务5):MAGNETIC_ELEMENTS 由集合改为 {元素: 初猜比矩μB} dict,
    # 供 build_magmom 差异化取矩(3d/4d/5d/4f 磁矩量级不同,统一 5μB 是粗糙初猜)。
    assert MAGNETIC_ELEMENTS == {
        'V': 3, 'Cr': 5, 'Mn': 5, 'Fe': 4, 'Co': 3, 'Ni': 2, 'Cu': 1,
        'Mo': 3, 'W': 2, 'Ce': 1, 'Gd': 7,
    }
    # dict 成员判定仍按键工作(has_magnetic/validate 依赖此语义,不破)
    assert 'Fe' in MAGNETIC_ELEMENTS and 'Zn' not in MAGNETIC_ELEMENTS
