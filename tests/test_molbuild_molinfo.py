"""molbuild.molinfo 测试:Hill 序化学式 / 电子数 / 分子量 / 多重度初猜 / 元素表(纯 python)。"""
import pytest

from vcstudio.molbuild import molinfo


def test_formula_hill_with_carbon():
    # 乙醇 C2H6O:含碳 → C 在前、H 次之,余元素字母序
    assert molinfo.formula(['C', 'C', 'O'] + ['H'] * 6) == 'C2H6O'


def test_formula_hill_carbon_no_hydrogen():
    assert molinfo.formula(['C', 'O', 'O']) == 'CO2'


def test_formula_no_carbon_alphabetical():
    # 硫酸根骨架无碳 → 全字母序(O 在 S 前);H 也参与字母序
    assert molinfo.formula(['S', 'O', 'O', 'O', 'O']) == 'O4S'
    assert molinfo.formula(['H', 'H', 'O']) == 'H2O'
    assert molinfo.formula(['O', 'H', 'Na']) == 'HNaO'


def test_formula_singleton_no_digit_and_empty():
    assert molinfo.formula(['Fe']) == 'Fe'
    assert molinfo.formula([]) == ''


def test_formula_normalises_case():
    assert molinfo.formula(['c', 'o', 'o']) == 'CO2'


def test_n_electrons_neutral_and_charged():
    assert molinfo.n_electrons(['H', 'H', 'O']) == 10
    assert molinfo.n_electrons(['H', 'H', 'O'], charge=1) == 9      # 失一电子
    assert molinfo.n_electrons(['H', 'H', 'O'], charge=-1) == 11    # 得一电子


def test_molecular_mass_water():
    assert molinfo.molecular_mass(['H', 'H', 'O']) == pytest.approx(18.015, abs=1e-3)


def test_mol_summary_even_electrons_singlet():
    s = molinfo.mol_summary(['H', 'H', 'O'], None, 0)
    assert s['formula'] == 'H2O'
    assert s['n_atoms'] == 3
    assert s['n_electrons'] == 10
    assert s['mass_amu'] == pytest.approx(18.015, abs=1e-3)
    assert s['suggested_multiplicity'] == 1
    assert '初猜' in s['multiplicity_note']


def test_mol_summary_odd_electrons_doublet():
    # 甲基自由基 CH3:6+3=9 电子(奇)→ 多重度初猜 2
    s = molinfo.mol_summary(['C', 'H', 'H', 'H'], None, 0)
    assert s['n_electrons'] == 9
    assert s['suggested_multiplicity'] == 2


def test_mol_summary_charge_flips_parity():
    # 偶电子体系 +1 → 奇电子 → 多重度初猜 2
    s = molinfo.mol_summary(['H', 'H', 'O'], None, charge=1)
    assert s['suggested_multiplicity'] == 2


def test_atomic_number_and_symbol_roundtrip():
    assert molinfo.atomic_number('Fe') == 26
    assert molinfo.atomic_number('fe') == 26          # 大小写容错
    assert molinfo.element_symbol(26) == 'Fe'
    assert molinfo.element_symbol(1) == 'H'
    assert molinfo.element_symbol(molinfo.MAX_Z) == 'Rn'


def test_element_mass_and_unknown_raises():
    assert molinfo.element_mass('C') == pytest.approx(12.011)
    with pytest.raises(ValueError, match='未登记'):
        molinfo.atomic_number('Xx')
    with pytest.raises(ValueError, match='未登记'):
        molinfo.element_mass('Zz')
    with pytest.raises(ValueError, match='越界'):
        molinfo.element_symbol(0)
    with pytest.raises(ValueError, match='越界'):
        molinfo.element_symbol(molinfo.MAX_Z + 1)


def test_n_electrons_unknown_element_raises():
    with pytest.raises(ValueError, match='未登记'):
        molinfo.n_electrons(['C', 'Xx'])
