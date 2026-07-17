"""参考态与稳定性判据测试(F4 后半 + F24):参考规格 / 缓存口径守卫 / Eb·Ef·σ 手算。"""
import pytest

from vcstudio.generate import poscar
from vcstudio.generate.structure_view import parse_positions
from vcstudio.project import references as R


# ── 参考态规格 ───────────────────────────────────────────────────────────────
def test_reference_spec_bcc_li():
    spec = R.reference_spec('bcc_li')
    assert spec['kind'] == 'bcc_li'
    assert poscar.parse_poscar_species(spec['poscar']) == (['Li'], [2])   # 2 原子惯用胞
    assert 'k' in spec['note'].lower() or '静' in spec['note']


def test_reference_spec_isolated_atom_magnetic_hint():
    spec = R.reference_spec('isolated_atom', element='Fe')
    p = parse_positions(spec['poscar'])
    assert len(p['coords']) == 1
    assert 'ISPIN=2' in spec['spin_hint']                  # Fe 磁性 → 显式自旋
    assert '磁性' in spec['spin_hint']                     # 磁性专属措辞
    # 非对称盒(破简并):三条边不等长
    cell = p['cell']
    assert cell[0][0] != pytest.approx(cell[1][1])
    assert cell[1][1] != pytest.approx(cell[2][2])


def test_reference_spec_isolated_atom_nonmagnetic():
    spec = R.reference_spec('isolated_atom', element='Al')
    assert '磁性' not in spec['spin_hint']                 # Al 非磁 → 无磁性专属措辞
    assert '自旋' in spec['spin_hint']


def test_reference_spec_molecule_delegates():
    spec = R.reference_spec('molecule', name='S8')
    assert spec['formula'] == 'S8'
    assert poscar.parse_poscar_species(spec['poscar']) == (['S'], [8])


def test_reference_spec_errors():
    with pytest.raises(ValueError, match='isolated_atom'):
        R.reference_spec('isolated_atom')                  # 缺 element
    with pytest.raises(ValueError, match='molecule'):
        R.reference_spec('molecule')                       # 缺 name
    with pytest.raises(ValueError, match='未知参考态'):
        R.reference_spec('quark')


# ── 参考能缓存 + 口径守卫 ────────────────────────────────────────────────────
def test_reference_cache_register_lookup(tmp_path):
    c = R.reference_cache(tmp_path)
    c.register('S8_gas', energy=-123.45, job_dir='/jobs/S8', fingerprint_hash='fp1')
    e = c.lookup('S8_gas')
    assert e['energy'] == pytest.approx(-123.45)
    assert e['job_dir'] == '/jobs/S8'
    assert e['fingerprint_hash'] == 'fp1'
    assert e['warnings'] == []
    assert c.lookup('missing') is None
    assert (tmp_path / 'references.yaml').is_file()         # 已持久化


def test_reference_cache_persists_across_instances(tmp_path):
    R.reference_cache(tmp_path).register('bcc_Li', energy=-1.9, fingerprint_hash='fpL')
    reopened = R.reference_cache(tmp_path)
    assert reopened.lookup('bcc_Li')['energy'] == pytest.approx(-1.9)
    assert 'bcc_Li' in reopened.all()


def test_reference_cache_fingerprint_guard(tmp_path):
    c = R.reference_cache(tmp_path)
    c.register('N_atom', energy=-3.1, fingerprint_hash='fpA')
    # 同口径 → 无告警
    assert c.lookup('N_atom', fingerprint_hash='fpA')['warnings'] == []
    # 异口径 → 中文告警"不一致 / 不可比"
    w = c.lookup('N_atom', fingerprint_hash='fpB')['warnings']
    assert len(w) == 1
    assert '不一致' in w[0] and '不可比' in w[0]


def test_reference_cache_no_guard_when_stored_fp_absent(tmp_path):
    c = R.reference_cache(tmp_path)
    c.register('X', energy=-1.0)                            # 未登记指纹
    assert c.lookup('X', fingerprint_hash='anything')['warnings'] == []


# ── 稳定性判据(F24)手算对拍 ────────────────────────────────────────────────
def test_binding_energy_formula():
    # Eb = E_sac − E_sub − E_atom = −100 −(−90) −(−5) = −5
    assert R.binding_energy(-100.0, -90.0, -5.0) == pytest.approx(-5.0)


def test_formation_energy_signed_counts():
    # Ef = −100 −(−80) − (4·(−3) + (−2)·(−9)) = −20 − (−12 + 18) = −26
    ef = R.formation_energy(-100.0, -80.0, {'N': -3.0, 'C': -9.0}, {'N': 4, 'C': -2})
    assert ef == pytest.approx(-26.0)


def test_formation_energy_missing_mu_raises():
    with pytest.raises(ValueError, match='化学势'):
        R.formation_energy(-100.0, -80.0, {'N': -3.0}, {'N': 4, 'C': -2})


def test_cohesive_energy_table():
    assert R.cohesive_energy('Fe') == pytest.approx(4.28)
    assert R.cohesive_energy('W') == pytest.approx(8.90)
    with pytest.raises(ValueError, match='内聚能'):
        R.cohesive_energy('Xx')


def test_stability_verdict_stable():
    # σ = −Eb/Ecoh = −(−5)/4.28 = 1.168 > 1 → 稳
    v = R.stability_verdict(-5.0, R.cohesive_energy('Fe'))
    assert v['sigma'] == pytest.approx(1.1682, abs=1e-4)
    assert v['stable'] is True
    assert '判稳' in v['note']


def test_stability_verdict_unstable():
    # σ = 2/4.28 = 0.467 ≤ 1 → 不稳
    v = R.stability_verdict(-2.0, 4.28)
    assert v['sigma'] == pytest.approx(0.4673, abs=1e-4)
    assert v['stable'] is False
    assert '不稳' in v['note']


def test_stability_verdict_boundary_strict():
    # σ = 1.0 恰好 → 不判稳(严格 >1)
    v = R.stability_verdict(-4.28, 4.28)
    assert v['sigma'] == pytest.approx(1.0)
    assert v['stable'] is False


def test_stability_verdict_bad_ecoh_raises():
    with pytest.raises(ValueError, match='内聚能'):
        R.stability_verdict(-5.0, 0.0)
