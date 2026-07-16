"""频率/ZPE/熵热校正测试(project.thermo)+ discharge_path 的 g_corr 接线。"""
import math

import pytest

from vcstudio.project import thermo
from vcstudio.project.freeenergy import discharge_path

_OUTCAR_FREQ = """\
 Eigenvectors and eigenvalues of the dynamical matrix
 ----------------------------------------------------
   1 f  =   98.309520 THz   617.699596 2PI*THz 3279.156295 cm-1   406.564208 meV
   2 f  =   48.500000 THz   304.734000 2PI*THz 1617.800000 cm-1   200.600000 meV
   3 f  =   10.000000 THz    62.832000 2PI*THz  333.560000 cm-1    41.360000 meV
   4 f/i=    0.541996 THz     3.405475 2PI*THz   18.079172 cm-1     2.241701 meV
"""


def test_parse_outcar_frequencies_splits_real_imag():
    real, imag, imag_cm = thermo.parse_outcar_frequencies(_OUTCAR_FREQ)
    assert len(real) == 3 and len(imag) == 1
    assert real[0] == pytest.approx(406.564208)
    assert imag[0] == pytest.approx(2.241701)
    assert imag_cm[0] == pytest.approx(18.079172)


def test_parse_no_frequencies_returns_empty():
    assert thermo.parse_outcar_frequencies('normal relax OUTCAR text') == ([], [], [])


def test_harmonic_thermo_zpe_is_half_sum():
    # 理由:harmonic_thermo 新增低频地板后多返回一个 meta(第4项);测试频率均 >50cm⁻¹
    # (333/1618/3279 cm⁻¹)不触发地板,数值基线不变,仅解包多一项。
    real = [406.564208, 200.6, 41.36]
    zpe, u_th, ts, meta = thermo.harmonic_thermo(real, 298.15)
    assert zpe == pytest.approx(sum(real) / 2000.0, rel=1e-9)   # Σ hν/2 (meV→eV)
    assert ts > 0 and u_th > 0
    assert meta['n_floored'] == 0 and meta['floored_cm1'] == []  # 无低频 → 不抬频
    # 高频模在室温几乎不贡献熵/热占据;41 meV(x≈1.6)是主要贡献者
    _, u_high_only, ts_high_only, _m = thermo.harmonic_thermo([406.564208], 298.15)
    assert ts_high_only < 1e-6 and u_high_only < 1e-6


def test_harmonic_thermo_entropy_formula_single_mode():
    """单模数值口径核对:x = hv/kT;U = hv/(e^x−1);S/kB = x/(e^x−1) − ln(1−e^−x)。"""
    mev = 25.0                                           # ≈201.6 cm⁻¹,不触发 50cm⁻¹ 地板
    T = 300.0
    zpe, u_th, ts, _meta = thermo.harmonic_thermo([mev], T)
    hv = mev / 1000.0
    x = hv / (thermo.KB_EV * T)
    s_kb = x / math.expm1(x) - math.log1p(-math.exp(-x))
    assert ts == pytest.approx(thermo.KB_EV * T * s_kb, rel=1e-9)
    assert u_th == pytest.approx(hv / math.expm1(x), rel=1e-9)   # U_vib 热占据项
    assert zpe == pytest.approx(hv / 2)


def test_harmonic_thermo_low_freq_floor():
    """低频实模:熵/热能项按地板(50cm⁻¹)计,ZPE 用原频;meta 记被抬频。"""
    low_cm1 = 20.0
    low_mev = low_cm1 * thermo.CM1_TO_EV * 1000.0        # 20 cm⁻¹ 对应 meV
    zpe0, u0, ts0, m0 = thermo.harmonic_thermo([low_mev], 298.15, freq_floor_cm1=0.0)
    zpe1, u1, ts1, m1 = thermo.harmonic_thermo([low_mev], 298.15, freq_floor_cm1=50.0)
    assert m0['n_floored'] == 0                          # 关地板 → 不抬
    assert m1['n_floored'] == 1 and m1['floored_cm1'] == [pytest.approx(20.0, abs=0.05)]
    assert zpe1 == pytest.approx(zpe0)                   # ZPE 用原频,地板不改
    assert zpe1 == pytest.approx(low_mev / 2000.0)
    assert ts1 < ts0 and u1 < u0                         # 抬频 → 熵/热占据更小
    # 抬到 50cm⁻¹ 等价于直接用 50cm⁻¹ 算熵/热能
    floor_mev = 50.0 * thermo.CM1_TO_EV * 1000.0
    _z, u_ref, ts_ref, _m = thermo.harmonic_thermo([floor_mev], 298.15, freq_floor_cm1=0.0)
    assert ts1 == pytest.approx(ts_ref) and u1 == pytest.approx(u_ref)


def test_low_freq_floor_boundary_and_load_corrections(tmp_path):
    """高于地板者不动;analyze_outcar/load_corrections 透传 n_floored/被抬频。"""
    hi_mev = 100.0 * thermo.CM1_TO_EV * 1000.0           # 100 cm⁻¹ > 50 → 不抬
    _z, _u, _t, meta = thermo.harmonic_thermo([hi_mev], 298.15, freq_floor_cm1=50.0)
    assert meta['n_floored'] == 0
    d = tmp_path / 'freq_low'
    d.mkdir()
    (d / 'OUTCAR').write_text(
        ' Eigenvectors and eigenvalues of the dynamical matrix\n'
        '   1 f  =   10.0 THz   62.8 2PI*THz  333.560000 cm-1   41.360000 meV\n'
        '   2 f  =    0.5 THz    3.4 2PI*THz   18.079172 cm-1    2.241701 meV\n',
        encoding='utf-8')
    corr = thermo.load_corrections({'X': str(d)}, freq_floor_cm1=50.0)
    assert corr['X']['n_floored'] == 1                   # 18 cm⁻¹ 被抬,333 cm⁻¹ 不抬
    assert corr['X']['floored_cm1'] == [18.1]


def test_g_corr_two_modes_same_frequencies():
    """同一组频率:'zpe_ts' 与 'ase' 给不同 g_corr;默认 = ZPE−TS(论文口径)。"""
    real = [406.564208, 200.6, 41.36]
    zpe, u_th, ts, _meta = thermo.harmonic_thermo(real, 298.15)
    r = thermo.VibResult(real_mev=real, zpe_ev=zpe, u_thermal_ev=u_th, ts_ev=ts)
    assert r.g_corr('zpe_ts') == pytest.approx(zpe - ts)
    assert r.g_corr('ase') == pytest.approx(zpe + u_th - ts)
    assert r.g_corr('ase') - r.g_corr('zpe_ts') == pytest.approx(u_th)
    assert u_th > 0                                   # 室温下两口径确实不同
    assert r.g_corr() == r.g_corr('zpe_ts')           # 不带参默认论文口径
    assert r.g_corr_ev == pytest.approx(zpe - ts)     # property = 默认口径,复现基准
    with pytest.raises(ValueError, match='zpe_ts'):
        r.g_corr('nonsense')


def test_analyze_outcar_and_load_corrections(tmp_path):
    d = tmp_path / 'freq_Li2S'
    d.mkdir()
    (d / 'OUTCAR').write_text(_OUTCAR_FREQ, encoding='utf-8')
    r = thermo.analyze_outcar(d / 'OUTCAR')
    assert r is not None
    assert r.n_imag == 1
    assert r.g_corr_ev == pytest.approx(r.zpe_ev - r.ts_ev)

    corr = thermo.load_corrections({'Li2S': str(d), 'missing': str(tmp_path / 'nope')})
    assert 'Li2S' in corr and 'missing' not in corr
    assert corr['Li2S']['n_imag'] == 1
    assert corr['Li2S']['imag_cm1'] == [18.1]
    assert corr['Li2S']['g_corr'] == pytest.approx(
        corr['Li2S']['zpe'] - corr['Li2S']['ts'], abs=2e-6)      # 默认 zpe_ts 口径
    # mode='ase':同一批 OUTCAR,g_corr 多出 u_thermal 项
    corr_ase = thermo.load_corrections({'Li2S': str(d)}, mode='ase')
    assert corr_ase['Li2S']['g_corr'] == pytest.approx(
        corr['Li2S']['g_corr'] + corr['Li2S']['u_thermal'], abs=2e-6)
    assert corr_ase['Li2S']['u_thermal'] > 0


def test_analyze_outcar_none_for_relax(tmp_path):
    p = tmp_path / 'OUTCAR'
    p.write_text('reached required accuracy\n', encoding='utf-8')
    assert thermo.analyze_outcar(p) is None


# ── discharge_path 的 g_corr 接线 ────────────────────────────────────────────
_SYS_E = {'S8': -100.0, 'Li2S8': -110.0, 'Li2S6': -95.0, 'Li2S4': -80.0,
          'Li2S2': -60.0, 'Li2S': -40.0}
_MOL_E = {'Li2S': -8.0, 'Li2S2': -13.0, 'S8': -32.0}


def test_discharge_path_g_corr_shifts_steps():
    base = discharge_path(_SYS_E, _MOL_E)
    assert base['thermo_corrected'] is False
    corr = {sp: 0.1 for sp in _SYS_E}                  # 全员等量校正 → ΔG 不变
    shifted = discharge_path(_SYS_E, _MOL_E, g_corr=corr)
    assert shifted['thermo_corrected'] is True
    for a, b in zip(base['steps'], shifted['steps']):
        assert b['G'] == pytest.approx(a['G'])         # 等量校正相消(参照 S8*)
    # 只校正末态 → 末台阶抬高 0.2
    corr2 = {'Li2S': 0.2}
    shifted2 = discharge_path(_SYS_E, _MOL_E, g_corr=corr2)
    assert shifted2['steps'][-1]['G'] == pytest.approx(base['steps'][-1]['G'] + 0.2)
    assert shifted2['steps'][0]['G'] == pytest.approx(0.0)
