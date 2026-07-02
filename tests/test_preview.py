"""生成页即时预览纯函数测试(logic.poscar_preview / incar_preview,不开 Tk)。"""
from vcstudio.gui.logic import poscar_preview, incar_preview


def _write_poscar(tmp_path):
    p = tmp_path / 'POSCAR'
    p.write_text(
        'C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
        encoding='utf-8')
    return str(p)


def _write_lib(tmp_path):
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   ENMAX  =  273.214; ENMIN = 200.000 eV\n',
        encoding='utf-8')
    return str(lib)


def test_poscar_preview_summary_and_kpoints(tmp_path):
    p = _write_poscar(tmp_path)
    txt = poscar_preview(p, 'slab')
    assert '体系:C atom' in txt
    assert 'C 1' in txt and '共 1 原子' in txt
    assert '10.00' in txt                      # 晶格边长
    assert '5 × 5 × 1' in txt                  # 10Å 盒 slab:ceil(3.33)=4→奇数化 5,kz=1
    assert '1 × 1 × 1' in poscar_preview(p, 'molecule')


def test_poscar_preview_missing_and_vasp4(tmp_path):
    assert '选择 POSCAR' in poscar_preview('')
    bad = tmp_path / 'P4'
    bad.write_text('t\n1.0\n10 0 0\n0 10 0\n0 0 10\n1\nCartesian\n0 0 0\n',
                   encoding='utf-8')
    assert 'VASP4' in poscar_preview(str(bad))


def test_incar_preview_completion_and_warnings(tmp_path):
    p, lib = _write_poscar(tmp_path), _write_lib(tmp_path)
    inc = tmp_path / 'INCAR'
    inc.write_text('ISMEAR = 0\n', encoding='utf-8')
    txt = incar_preview(str(inc), p, lib, validate=True)
    assert '将补全' in txt and 'ENCUT = 400' in txt   # 1.3×273.214→上取 50 → 400

    inc.write_text('ENCUT = 500\nISMEAR = 0\n', encoding='utf-8')
    assert '原文透传' in incar_preview(str(inc), p, lib, validate=True)


def test_incar_preview_degrades_gracefully(tmp_path):
    p = _write_poscar(tmp_path)
    inc = tmp_path / 'INCAR'
    inc.write_text('ISMEAR = 0\n', encoding='utf-8')
    # 校验关闭
    assert '严格照抄' in incar_preview(str(inc), p, 'whatever', validate=False)
    # 未选 POSCAR
    assert '选择 POSCAR' in incar_preview(str(inc), '', 'lib', validate=True)
    # 赝势库不可用 → 友好提示而非异常
    assert '无法预览补全' in incar_preview(str(inc), p, str(tmp_path / 'nolib'),
                                        validate=True)
    # 未设库
    assert '未设置赝势库' in incar_preview(str(inc), p, '', validate=True)
