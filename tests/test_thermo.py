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
    real = [406.564208, 200.6, 41.36]
    zpe, ts = thermo.harmonic_thermo(real, 298.15)
    assert zpe == pytest.approx(sum(real) / 2000.0, rel=1e-9)   # Σ hν/2 (meV→eV)
    assert ts > 0
    # 高频模在室温几乎不贡献熵;41 meV(x≈1.6)是主要贡献者
    _, ts_high_only = thermo.harmonic_thermo([406.564208], 298.15)
    assert ts_high_only < 1e-6


def test_harmonic_thermo_entropy_formula_single_mode():
    """单模数值口径核对:x = hv/kT;S/kB = x/(e^x−1) − ln(1−e^−x)。"""
    mev = 25.0
    T = 300.0
    zpe, ts = thermo.harmonic_thermo([mev], T)
    hv = mev / 1000.0
    x = hv / (thermo.KB_EV * T)
    s_kb = x / math.expm1(x) - math.log1p(-math.exp(-x))
    assert ts == pytest.approx(thermo.KB_EV * T * s_kb, rel=1e-9)
    assert zpe == pytest.approx(hv / 2)


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
