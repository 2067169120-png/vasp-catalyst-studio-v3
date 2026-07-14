"""C1 收敛解析纯函数测试。真实 VASP 片段夹具,数值手算核对。"""
import math

import pytest

from vcstudio.cluster import convergence


# ── 夹具:2 离子步 OSZICAR(手算:step1 E0=-85.018175 scf=2;step2 E0=-85.12 scf=3)──
OSZICAR_2STEP = """\
       N       E                     dE             d eps       ncg     rms
DAV:   1     0.11600000E+03   0.116E+03   0.116E+03   864   0.5
DAV:   2    -0.84943000E+02  -0.200E+03  -0.200E+03   912   0.1
   1 F= -.85018175E+02 E0= -.85018175E+02  d E =-.850182E+02
DAV:   1    -0.85000000E+02  -0.100E-01  -0.100E-01   700   0.01
DAV:   2    -0.85110000E+02  -0.500E-02  -0.500E-02   680   0.005
DAV:   3    -0.85120000E+02  -0.100E-03  -0.100E-03   660   0.001
   2 F= -.85120000E+02 E0= -.85120000E+02  d E =-.101825E+00
"""

# ── 夹具:对应 OUTCAR 力块(step1 |F|max=0.5;step2 |F|max=0.05)──
OUTCAR_2STEP = """\
 some header noise
 POSITION                                       TOTAL-FORCE (eV/Angst)
 -----------------------------------------------------------------------------------
      0.00000      0.00000      0.00000         0.300000     0.400000     0.000000
      1.00000      1.00000      1.00000         0.000000     0.000000     0.100000
 -----------------------------------------------------------------------------------
    total drift:                                0.000001     0.000001     0.000001
 intermediate stuff
 POSITION                                       TOTAL-FORCE (eV/Angst)
 -----------------------------------------------------------------------------------
      0.00000      0.00000      0.00000         0.000000     0.030000     0.040000
      1.00000      1.00000      1.00000         0.010000     0.000000     0.000000
 -----------------------------------------------------------------------------------
    total drift:                                0.000000     0.000000     0.000000
"""


def test_parse_oszicar_two_steps():
    steps = convergence.parse_oszicar(OSZICAR_2STEP)
    assert len(steps) == 2
    assert steps[0]['step'] == 1
    assert steps[0]['E0'] == pytest.approx(-85.018175)
    assert steps[0]['dE'] is None
    assert steps[0]['scf_iters'] == 2
    assert steps[1]['step'] == 2
    assert steps[1]['E0'] == pytest.approx(-85.12)
    assert steps[1]['dE'] == pytest.approx(0.101825, abs=1e-6)
    assert steps[1]['scf_iters'] == 3


def test_parse_outcar_fmax():
    fmax = convergence.parse_outcar_fmax(OUTCAR_2STEP)
    assert len(fmax) == 2
    assert fmax[0] == pytest.approx(0.5)   # sqrt(0.3^2+0.4^2)
    assert fmax[1] == pytest.approx(0.05)  # sqrt(0.03^2+0.04^2)


def test_convergence_series_full():
    s = convergence.convergence_series(OSZICAR_2STEP, OUTCAR_2STEP)
    assert s['steps'] == [1, 2]
    assert s['E0'] == pytest.approx([-85.018175, -85.12])
    assert s['dE'][0] is None
    assert s['dE'][1] == pytest.approx(0.101825, abs=1e-6)
    assert s['fmax'] == pytest.approx([0.5, 0.05])
    assert s['scf_iters'] == [2, 3]
    assert s['have_forces'] is True
    assert s['notes'] == []


def test_convergence_series_no_outcar():
    s = convergence.convergence_series(OSZICAR_2STEP, None)
    assert s['steps'] == [1, 2]
    assert s['have_forces'] is False
    assert s['fmax'] == [None, None]
    assert any('OUTCAR' in n for n in s['notes'])


def test_convergence_series_single_step():
    single = """\
DAV:   1    -0.10000000E+02  -0.1E+02  -0.1E+02   100   0.5
   1 F= -.10000000E+02 E0= -.10000000E+02  d E =-.100000E+02
"""
    s = convergence.convergence_series(single, None)
    assert s['steps'] == [1]
    assert s['E0'] == pytest.approx([-10.0])
    assert s['dE'] == [None]


def test_convergence_series_empty_and_garbage():
    for bad in ('', '   ', 'not an oszicar at all\nrandom text\n'):
        s = convergence.convergence_series(bad, None)
        assert s['steps'] == []
        assert s['E0'] == []
        assert s['have_forces'] is False
        assert s['notes']  # 有说明,不静默


def test_parse_oszicar_skips_bad_lines():
    """夹层坏行不应中断解析,好步照常产出。"""
    text = """\
DAV:   1    -0.85000000E+02  -0.1E-01  -0.1E-01   700   0.01
GARBAGE F= not-a-number E0= also-bad d E = nope
DAV:   1    -0.86000000E+02  -0.1E-01  -0.1E-01   700   0.01
   1 F= -.86000000E+02 E0= -.86000000E+02  d E =-.860000E+02
"""
    steps = convergence.parse_oszicar(text)
    # 坏 F= 行(E0 解析失败)被跳过,只留 1 个有效离子步
    assert len(steps) == 1
    assert steps[0]['E0'] == pytest.approx(-86.0)


def test_parse_outcar_no_force_blocks():
    assert convergence.parse_outcar_fmax('header only, no forces\n') == []
    assert convergence.parse_outcar_fmax('') == []
