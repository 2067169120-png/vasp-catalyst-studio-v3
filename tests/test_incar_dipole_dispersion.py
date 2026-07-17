"""INCAR 偶极校正建议(F6)与色散一致性审计(F13):两个独立新函数。"""
from vcstudio.generate import incar_builder as ib

# 原子聚在盒底的 slab(z 质心 3,盒中心 15 → 强不对称)
_ASYM_SLAB = """asym slab
1.0
10.0 0.0 0.0
0.0 10.0 0.0
0.0 0.0 30.0
C
3
Cartesian
5.0 5.0 2.0
5.0 5.0 3.0
5.0 5.0 4.0
"""

# 原子居中的 slab(z 质心 15 = 盒中心 → 对称)
_SYM_SLAB = """sym slab
1.0
10.0 0.0 0.0
0.0 10.0 0.0
0.0 0.0 30.0
C
3
Cartesian
5.0 5.0 14.0
5.0 5.0 15.0
5.0 5.0 16.0
"""


# ── 偶极校正(F6) ───────────────────────────────────────────────────────────
def test_dipole_asymmetric_slab_suggests_keys():
    keys = ib.dipole_correction_keys(_ASYM_SLAB, 'slab')
    assert keys['LDIPOL'] == '.TRUE.'
    assert keys['IDIPOL'] == '3'
    # 质心分数坐标:x=y=0.5,z=3/30=0.1
    assert keys['DIPOL'] == '0.5000 0.5000 0.1000'


def test_dipole_symmetric_slab_empty():
    assert ib.dipole_correction_keys(_SYM_SLAB, 'slab') == {}


def test_dipole_only_for_slab_calc_type():
    # 非 slab(分子/体相)不建议,即使结构不对称
    assert ib.dipole_correction_keys(_ASYM_SLAB, 'molecule') == {}
    assert ib.dipole_correction_keys(_ASYM_SLAB, 'bulk') == {}


def test_dipole_garbage_poscar_silent_empty():
    assert ib.dipole_correction_keys('not a poscar', 'slab') == {}
    assert ib.dipole_correction_keys('', 'slab') == {}


def test_dipole_dipol_is_three_floats():
    keys = ib.dipole_correction_keys(_ASYM_SLAB, 'slab')
    assert len(keys['DIPOL'].split()) == 3
    for tok in keys['DIPOL'].split():
        float(tok)                                         # 可解析为浮点


# ── 色散审计(F13) ──────────────────────────────────────────────────────────
def test_dispersion_all_same_ok():
    res = ib.dispersion_audit(['IVDW = 11', 'IVDW=11\nENCUT=500'])
    assert res['ok'] is True
    assert res['ivdw'] == 11


def test_dispersion_all_absent_ok():
    res = ib.dispersion_audit(['ENCUT=500', 'PREC=Accurate'])
    assert res['ok'] is True
    assert res['ivdw'] is None
    assert '均未设' in res['detail']


def test_dispersion_partial_missing_flags():
    res = ib.dispersion_audit(['IVDW=11', 'ENCUT=500', 'IVDW=11'])
    assert res['ok'] is False
    assert '第 2 份缺 IVDW' in res['detail']
    assert '不可比' in res['detail']


def test_dispersion_mixed_values_flags():
    res = ib.dispersion_audit(['IVDW=11', 'IVDW=12'])
    assert res['ok'] is False
    assert 'IVDW=11' in res['detail'] and 'IVDW=12' in res['detail']


def test_dispersion_single_incar_ok():
    assert ib.dispersion_audit(['IVDW=12'])['ok'] is True
