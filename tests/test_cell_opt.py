"""晶胞优化测试(generate.cell_opt):ISIF=3 / IBRION 修正 / NSW 保证 / Pulay warning /
ENCUT 抬高 / 电子学保留 / manifest。"""
import pytest

from vcstudio.generate import cell_opt
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared import manifest as manifest_mod

_BULK = """bulk Cu
1.0
3.5 0.0 0.0
0.0 3.5 0.0
0.0 0.0 3.5
Cu
1
Direct
0.0 0.0 0.0
"""
_INCAR = "ENCUT = 400\nGGA = PE\nISPIN = 2\nMAGMOM = 1*0\nIBRION = 2\nNSW = 80\nISIF = 2\nEDIFFG = -0.02\n"


def _make_src(tmp_path, incar=_INCAR, contcar=_BULK):
    d = tmp_path / 'relax'
    d.mkdir()
    if contcar is not None:
        (d / 'CONTCAR').write_text(contcar, encoding='utf-8')
    if incar is not None:
        (d / 'INCAR').write_text(incar, encoding='utf-8')
    (d / 'KPOINTS').write_text("Automatic\n0\nGamma\n7 7 7\n0 0 0\n", encoding='utf-8')
    (d / 'POTCAR').write_text("PAW_PBE Cu\n", encoding='utf-8')
    return d


def test_cellopt_sets_isif3(tmp_path):
    src = _make_src(tmp_path)
    cell_opt.build_cellopt_job(str(src), str(tmp_path / 'co'))
    d = parse_incar((tmp_path / 'co' / 'INCAR').read_text())
    assert d['ISIF'] == 3


def test_cellopt_preserves_electronic_keys(tmp_path):
    src = _make_src(tmp_path)
    cell_opt.build_cellopt_job(str(src), str(tmp_path / 'co'))
    d = parse_incar((tmp_path / 'co' / 'INCAR').read_text())
    assert d['ENCUT'] == 400 and d['GGA'] == 'PE' and d['ISPIN'] == 2 and d['MAGMOM'] == '1*0'


def test_cellopt_pulay_warning_default(tmp_path):
    src = _make_src(tmp_path)
    res = cell_opt.build_cellopt_job(str(src), str(tmp_path / 'co'))
    assert any('Pulay' in w for w in res['warnings'])
    # 默认不改 ENCUT
    assert parse_incar((tmp_path / 'co' / 'INCAR').read_text())['ENCUT'] == 400


def test_cellopt_bump_encut(tmp_path):
    src = _make_src(tmp_path)
    cell_opt.build_cellopt_job(str(src), str(tmp_path / 'co'), bump_encut=True, encut_scale=1.3)
    d = parse_incar((tmp_path / 'co' / 'INCAR').read_text())
    assert d['ENCUT'] == 520                              # ceil(400*1.3/10)*10 = 520


def test_cellopt_static_source_gets_positive_nsw(tmp_path):
    # 源为静态(NSW=0/IBRION=-1)→ 补正 NSW 且 IBRION 改 2
    src = _make_src(tmp_path, incar="ENCUT = 400\nNSW = 0\nIBRION = -1\n")
    cell_opt.build_cellopt_job(str(src), str(tmp_path / 'co'))
    d = parse_incar((tmp_path / 'co' / 'INCAR').read_text())
    assert d['NSW'] > 0 and d['IBRION'] == 2 and d['ISIF'] == 3


def test_cellopt_manifest(tmp_path):
    src = _make_src(tmp_path)
    cell_opt.build_cellopt_job(str(src), str(tmp_path / 'co'))
    m = manifest_mod.load_manifest(tmp_path / 'co')
    assert m['task_type'] == 'cellopt' and m['inputs']['isif'] == 3
    assert m['parent_job'] == str(src.resolve())


def test_cellopt_missing_incar_raises(tmp_path):
    src = _make_src(tmp_path, incar=None)
    with pytest.raises(ValueError, match='INCAR'):
        cell_opt.build_cellopt_job(str(src), str(tmp_path / 'co'))
