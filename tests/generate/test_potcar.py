"""generate/potcar.py:变体映射 + 本地拼接 + ENMAX≤ENCUT 硬校验。使用 mini_lib fixture。"""
import pytest

from vcstudio.generate import potcar


def test_variant_maps_known_elements():
    assert potcar.variant('Fe') == 'Fe'
    assert potcar.variant('V') == 'V_sv'
    assert potcar.variant('Ta') == 'Ta_pv'


def test_variant_unregistered_raises():
    with pytest.raises(potcar.PotcarError):
        potcar.variant('Xx')


def test_read_enmax(mini_lib):
    assert potcar.read_enmax('C', mini_lib) == 400.0
    assert potcar.read_enmax('Li', mini_lib) == 140.0


def test_read_enmax_missing_file_raises(mini_lib):
    with pytest.raises(potcar.PotcarError):
        potcar.read_enmax('Ti_pv', mini_lib)  # 不在 mini 库


def test_build_potcar_concatenation_order(mini_lib):
    # C(ENMAX400) 在 S(ENMAX260) 之前 → 拼接文本中 400 出现在 260 之前
    text = potcar.build_potcar(['C', 'S'], encut=400, lib_root=mini_lib)
    assert text.find('400.000') < text.find('260.000')
    # 反序则相反
    rev = potcar.build_potcar(['S', 'C'], encut=400, lib_root=mini_lib)
    assert rev.find('260.000') < rev.find('400.000')


def test_build_potcar_enmax_exceeds_encut_raises(mini_lib):
    # Li_sv ENMAX=499 > 400 → PotcarError(绝不静默)
    with pytest.raises(potcar.PotcarError, match='ENMAX'):
        potcar.build_potcar(['Li'], encut=400, lib_root=mini_lib,
                            _force_variant={'Li': 'Li_sv'})


def test_build_potcar_missing_variant_raises(mini_lib):
    with pytest.raises(potcar.PotcarError):
        potcar.build_potcar(['Ti'], encut=400, lib_root=mini_lib)  # Ti_pv 不在库


def test_max_enmax_returns_maximum(mini_lib):
    assert potcar.max_enmax(['C', 'Li'], lib_root=mini_lib) == 400.0
    assert potcar.max_enmax(['Li', 'S'], lib_root=mini_lib) == 260.0
