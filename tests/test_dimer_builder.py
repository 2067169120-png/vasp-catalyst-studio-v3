"""Dimer 过渡态测试(generate.dimer_builder):VTST INCAR 键 / MODECAR 差向量·随机·归一 /
VTST 编译 warning / manifest / verify_saddle 接 thermo.classify_imaginary。"""
import math

import pytest

from vcstudio.generate import dimer_builder as db
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared import manifest as manifest_mod

_POSCAR = """TS guess
1.0
6.0 0.0 0.0
0.0 6.0 0.0
0.0 0.0 6.0
H
2
Cartesian
0.0 0.0 0.0
1.0 0.0 0.0
"""
_DISPLACED = """TS displaced
1.0
6.0 0.0 0.0
0.0 6.0 0.0
0.0 0.0 6.0
H
2
Cartesian
0.0 0.0 0.0
1.2 0.0 0.0
"""
_INCAR = "ENCUT = 400\nGGA = PE\nIBRION = 2\nNSW = 100\nISIF = 2\nEDIFFG = -0.02\n"


def _make_src(tmp_path, incar=_INCAR, contcar=_POSCAR):
    d = tmp_path / 'guess'
    d.mkdir()
    if contcar is not None:
        (d / 'CONTCAR').write_text(contcar, encoding='utf-8')
    if incar is not None:
        (d / 'INCAR').write_text(incar, encoding='utf-8')
    (d / 'KPOINTS').write_text("Automatic\n0\nGamma\n1 1 1\n0 0 0\n", encoding='utf-8')
    (d / 'POTCAR').write_text("PAW_PBE H\n", encoding='utf-8')
    return d


def test_dimer_incar_vtst_keys(tmp_path):
    src = _make_src(tmp_path)
    db.build_dimer_job(str(src), str(tmp_path / 'dm'))
    d = parse_incar((tmp_path / 'dm' / 'INCAR').read_text(encoding='utf-8'))
    assert d['ICHAIN'] == 2 and d['IBRION'] == 3 and d['POTIM'] == 0 and d['IOPT'] == 2


def test_dimer_preserves_electronic(tmp_path):
    src = _make_src(tmp_path)
    db.build_dimer_job(str(src), str(tmp_path / 'dm'))
    d = parse_incar((tmp_path / 'dm' / 'INCAR').read_text(encoding='utf-8'))
    assert d['ENCUT'] == 400 and d['GGA'] == 'PE'


def test_dimer_vtst_compile_warning(tmp_path):
    src = _make_src(tmp_path)
    res = db.build_dimer_job(str(src), str(tmp_path / 'dm'))
    assert any('VTST' in w and ('补丁' in w or '编译' in w) for w in res['warnings'])


def test_modecar_difference_vector_normalized():
    txt, method, warns = db.build_modecar(_POSCAR, _DISPLACED)
    assert method == 'difference' and not warns
    rows = [list(map(float, ln.split())) for ln in txt.strip().splitlines()]
    # 差向量 = 原子2 移动 (0.2,0,0),归一后应为单位长度
    norm = math.sqrt(sum(v * v for r in rows for v in r))
    assert norm == pytest.approx(1.0)
    assert rows[1][0] == pytest.approx(1.0)              # 全部位移集中在原子2的 x
    assert rows[0] == pytest.approx([0.0, 0.0, 0.0])


def test_modecar_random_when_no_displaced_deterministic():
    a, _m, _w = db.build_modecar(_POSCAR, None, seed=7)
    b, _m2, _w2 = db.build_modecar(_POSCAR, None, seed=7)
    assert a == b                                        # 同 seed 可复现
    rows = [list(map(float, ln.split())) for ln in a.strip().splitlines()]
    assert math.sqrt(sum(v * v for r in rows for v in r)) == pytest.approx(1.0)


def test_modecar_identical_configs_fallback_random():
    txt, method, warns = db.build_modecar(_POSCAR, _POSCAR)   # 差向量为零
    assert method == 'random' and any('回退' in w for w in warns)


def test_dimer_writes_modecar_and_manifest(tmp_path):
    src = _make_src(tmp_path)
    res = db.build_dimer_job(str(src), str(tmp_path / 'dm'), displaced_poscar=_DISPLACED)
    assert (tmp_path / 'dm' / 'MODECAR').is_file()
    assert res['modecar_method'] == 'difference'
    m = manifest_mod.load_manifest(tmp_path / 'dm')
    assert m['task_type'] == 'dimer' and m['inputs']['modecar_method'] == 'difference'


def test_dimer_missing_structure_raises(tmp_path):
    src = _make_src(tmp_path, contcar=None)
    (src / 'CONTCAR').unlink(missing_ok=True)
    with pytest.raises(ValueError, match='CONTCAR/POSCAR'):
        db.build_dimer_job(str(src), str(tmp_path / 'dm'))


# ── verify_saddle 接 thermo.classify_imaginary ─────────────────────────────────
_TS_OUTCAR = """ frequencies
   1 f  =   30.0 THz   188.0 2PI*THz  1000.0 cm-1   124.0 meV
   2 f/i=   15.0 THz    94.0 2PI*THz   500.0 cm-1    62.0 meV
"""
_MIN_OUTCAR = """ frequencies
   1 f  =   30.0 THz   188.0 2PI*THz  1000.0 cm-1   124.0 meV
   2 f  =   20.0 THz   126.0 2PI*THz   700.0 cm-1    87.0 meV
"""


def test_verify_saddle_valid_ts():
    r = db.verify_saddle(_TS_OUTCAR)                     # 恰一个大虚频
    assert r['verdict'] == 'valid_ts' and r['n_imag_large'] == 1


def test_verify_saddle_no_imag_invalid():
    r = db.verify_saddle(_MIN_OUTCAR)                    # 0 虚频 → 非过渡态
    assert r['verdict'] == 'invalid_ts'


def test_verify_saddle_no_freq_note():
    r = db.verify_saddle('no frequency lines here\n')
    assert r['verdict'] is None and '频率' in r['note']


def test_verify_saddle_reads_dir(tmp_path):
    d = tmp_path / 'freq'
    d.mkdir()
    (d / 'OUTCAR').write_text(_TS_OUTCAR, encoding='utf-8')
    r = db.verify_saddle(str(d))
    assert r['verdict'] == 'valid_ts'
