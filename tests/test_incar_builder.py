"""incar_builder / job_builder 回归测试:真实 INCAR 的行内说明文字。

老式 VASP INCAR 惯例:值后直接跟英文说明、不带 #/! 注释符,如
``ENCUT     = 400.0      cut-off energy (eV)``。VASP(Fortran)对标量
tag 只读首 token,故此类文件在 VASP 畅通;解析器必须兼容(验收实测踩坑)。
"""
import pytest

from vcstudio.generate.incar_builder import (
    parse_incar, build_magmom, has_magnetic, MAGNETIC_ELEMENTS, DEFAULT_MAGMOM,
)
from vcstudio.generate.job_builder import build_job_dir


def test_build_magmom_per_element_moments():
    # 每元素按比矩初猜,非磁性 0;顺序对齐 elements(= POTCAR/POSCAR 顺序)
    assert build_magmom(['Fe', 'O'], [2, 3]) == '2*4 3*0'      # Fe 比矩 4,O 非磁 0
    assert build_magmom(['Co', 'Ni', 'C'], [1, 1, 4]) == '1*3 1*2 4*0'
    assert build_magmom(['Gd'], [1]) == '1*7'                  # 4f 高矩
    # overrides 优先于比矩表
    assert build_magmom(['Fe'], [1], {'Fe': 1.5}) == '1*1.5'
    # counts 缺失/不等长 → None(降级)
    assert build_magmom(['Fe', 'O'], []) is None
    assert build_magmom(['Fe', 'O'], [1]) is None
    # has_magnetic 仍按元素键判定;DEFAULT_MAGMOM 兜底常量保留
    assert has_magnetic(['Fe', 'O']) and not has_magnetic(['C', 'O'])
    assert 'Fe' in MAGNETIC_ELEMENTS and DEFAULT_MAGMOM == 5


def test_scalar_with_trailing_prose_takes_first_token():
    text = (
        ' ENCUT     = 400.0      cut-off energy (eV)\n'
        ' ISPIN     = 1          spin polarized calculation?\n'
        ' ISMEAR    = 0          0:Gaussian -1:Fermi -5:Tetrahedron method\n'
        ' EDIFF     = 1E-5       stopping-criterion for electronic SC-loop\n'
        ' LORBIT    = 11         write DOSCAR and PROCAR\n'
    )
    d = parse_incar(text)
    assert d['ENCUT'] == 400.0
    assert d['ISPIN'] == 1
    assert d['ISMEAR'] == 0
    assert d['EDIFF'] == pytest.approx(1e-5)
    assert d['LORBIT'] == 11


def test_multivalue_numeric_string_is_preserved():
    # 首两个 token 都是数字 → 真·多值串,整体保留不拆
    d = parse_incar('LDAUU = 2 0 0\nMAGMOM = 16*0 2*5')
    assert d['LDAUU'] == '2 0 0'
    assert d['MAGMOM'] == '16*0 2*5'


def test_algo_f_with_prose_stays_string_not_bool():
    # ALGO = F 意为 Fast(字符串),带说明文字时绝不能被解析成 False
    d = parse_incar(' ALGO     = F         algorithm')
    assert isinstance(d['ALGO'], str)
    assert d['ALGO'].split()[0] == 'F'


def _make_fixture(tmp_path):
    """最小可生成环境:单元素 C 的假 POTCAR 库 + 单原子 POSCAR。"""
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   ENMAX  =  273.214; ENMIN = 200.000 eV\n',
        encoding='utf-8')
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(
        'C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
        encoding='utf-8')
    return str(poscar), str(lib)


def test_build_job_dir_accepts_prose_incar(tmp_path):
    # 验收实测的原始失败场景:ENCUT 带行内说明 → 曾报「ENCUT 非数字」
    poscar, lib = _make_fixture(tmp_path)
    incar = ' ENCUT     = 400.0      cut-off energy (eV)\n ISPIN = 1  spin?\n'
    res = build_job_dir(poscar, incar, str(tmp_path / 'out'),
                        calc_type='molecule', lib_root=lib)
    assert res['ok'] is True
    # 决策1 原文透传:输出 INCAR 保留原行(含说明文字)
    out_text = (tmp_path / 'out' / 'INCAR').read_text(encoding='utf-8')
    assert 'cut-off energy' in out_text


def test_encut_truly_non_numeric_still_raises(tmp_path):
    # 首 token 就不是数字 → 仍要报友好错误,不静默放行
    poscar, lib = _make_fixture(tmp_path)
    with pytest.raises(ValueError, match='ENCUT'):
        build_job_dir(poscar, 'ENCUT = abc def\n', str(tmp_path / 'out'),
                      calc_type='molecule', lib_root=lib)
