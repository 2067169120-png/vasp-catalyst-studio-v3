"""generate/job_builder.py:四件套落盘 + INCAR 原文透传+追加 + 冒泡/幂等。"""
import pytest

from vcstudio.generate import job_builder
from vcstudio.generate.potcar import PotcarError

# 含磁 slab(Fe/C,均在 mini_lib);scale=1,cell diag(3,3,15)
POSCAR_FEC = """Fe C slab
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
Fe C
1 4
Direct
0.0 0.0 0.0
0.1 0.1 0.1
"""

# 非磁(C/N)
POSCAR_CN = """C N box
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
C N
1 1
Direct
0.0 0.0 0.0
0.5 0.5 0.5
"""

# VASP4:无元素符号行
POSCAR_VASP4 = """title
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
2 4
Direct
"""

USER_INCAR = """# my tuned INCAR
ENCUT = 500
IBRION = 2
NSW = 200
EDIFF = 1E-6
"""


def _write_poscar(tmp_path, text, name='POSCAR'):
    p = tmp_path / name
    p.write_text(text, encoding='utf-8')
    return str(p)


def test_four_files_present(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_FEC)
    out = tmp_path / 'job1'
    res = job_builder.build_job_dir(pos, USER_INCAR, str(out),
                                    calc_type='slab', lib_root=mini_lib)
    assert res['ok'] is True
    assert res['elements'] == ['Fe', 'C']
    for name in ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR'):
        assert (out / name).is_file(), f'{name} 缺失'


def test_user_incar_preserved_verbatim_plus_append(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_FEC)
    out = tmp_path / 'job1'
    job_builder.build_job_dir(pos, USER_INCAR, str(out), lib_root=mini_lib)
    txt = (out / 'INCAR').read_text(encoding='utf-8')
    # 原文逐字保留(含注释、1E-6 不变形)
    assert '# my tuned INCAR' in txt
    assert 'NSW = 200' in txt
    assert 'EDIFF = 1E-6' in txt        # 关键:未被重写为 1e-06
    # 追加补全(Fe 磁性,原文无 MAGMOM)
    assert 'vcstudio 自动补全' in txt
    assert 'MAGMOM = 1*5 4*0' in txt
    assert 'ISPIN = 2' in txt


def test_kpoints_override(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_FEC)
    out = tmp_path / 'job1'
    res = job_builder.build_job_dir(pos, USER_INCAR, str(out),
                                    kpoints=[5, 5, 1], lib_root=mini_lib)
    assert res['kpoints'] == [5, 5, 1]
    assert '5 5 1' in (out / 'KPOINTS').read_text(encoding='utf-8')


def test_molecule_kpoints_gamma_only(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_CN)
    out = tmp_path / 'jobm'
    job_builder.build_job_dir(pos, 'ENCUT = 500\n', str(out),
                              calc_type='molecule', lib_root=mini_lib)
    assert '1 1 1' in (out / 'KPOINTS').read_text(encoding='utf-8')


def test_validate_false_no_append(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_FEC)
    out = tmp_path / 'job1'
    res = job_builder.build_job_dir(pos, USER_INCAR, str(out),
                                    validate=False, lib_root=mini_lib)
    txt = (out / 'INCAR').read_text(encoding='utf-8')
    assert 'MAGMOM' not in txt            # 关闭校验 → 不追加
    assert 'vcstudio 自动补全' not in txt
    assert res['warnings'] == []


def test_vasp4_poscar_raises(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_VASP4)
    with pytest.raises(ValueError):
        job_builder.build_job_dir(pos, 'ENCUT = 500\n', str(tmp_path / 'j'),
                                  lib_root=mini_lib)


def test_enmax_exceeds_encut_bubbles(tmp_path, mini_lib):
    # ENCUT=200 < C(400) → build_potcar 抛 PotcarError,build_job_dir 不吞
    pos = _write_poscar(tmp_path, POSCAR_CN)
    with pytest.raises(PotcarError):
        job_builder.build_job_dir(pos, 'ENCUT = 200\n', str(tmp_path / 'j'),
                                  lib_root=mini_lib)


def test_nonnumeric_encut_clear_error(tmp_path, mini_lib):
    # 用户 ENCUT 非数字 → 清晰 ValueError(提及 ENCUT),而非 cryptic float 崩溃(审 P0-1)
    pos = _write_poscar(tmp_path, POSCAR_CN)
    with pytest.raises(ValueError, match='ENCUT'):
        job_builder.build_job_dir(pos, 'ENCUT = auto\n', str(tmp_path / 'j'),
                                  lib_root=mini_lib)


def test_encut_zero_not_swallowed(tmp_path, mini_lib):
    # 用户 ENCUT=0 不被 or 链吞成 400 → 原样用于 ENMAX 检查 → PotcarError(审轮2 P1)
    pos = _write_poscar(tmp_path, POSCAR_CN)
    with pytest.raises(PotcarError):
        job_builder.build_job_dir(pos, {'ENCUT': 0}, str(tmp_path / 'j'),
                                  validate=False, lib_root=mini_lib)


def test_validate_false_without_encut_ok(tmp_path, mini_lib):
    # validate=False 且无 ENCUT:按 VASP 语义(默认 max ENMAX)放行,不注入 ENCUT 到 INCAR
    pos = _write_poscar(tmp_path, POSCAR_CN)
    out = tmp_path / 'j'
    res = job_builder.build_job_dir(pos, 'IBRION = 2\n', str(out),
                                    validate=False, lib_root=mini_lib)
    assert res['ok'] is True
    assert 'ENCUT' not in (out / 'INCAR').read_text(encoding='utf-8')


def test_bool_encut_clear_error(tmp_path, mini_lib):
    # ENCUT = .TRUE. 被 parse 成 bool → 清晰 ValueError,而非静默转 1(审轮3 P2-1)
    pos = _write_poscar(tmp_path, POSCAR_CN)
    with pytest.raises(ValueError, match='ENCUT'):
        job_builder.build_job_dir(pos, 'ENCUT = .TRUE.\n', str(tmp_path / 'j'),
                                  lib_root=mini_lib)


def test_idempotent_rerun(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_FEC)
    out = tmp_path / 'job1'
    job_builder.build_job_dir(pos, USER_INCAR, str(out), lib_root=mini_lib)
    # 再跑一次不报错(exist_ok),文件仍在
    res = job_builder.build_job_dir(pos, USER_INCAR, str(out), lib_root=mini_lib)
    assert res['ok'] is True
    assert (out / 'INCAR').is_file()


def test_incar_dict_input_serialized(tmp_path, mini_lib):
    pos = _write_poscar(tmp_path, POSCAR_CN)
    out = tmp_path / 'jobd'
    job_builder.build_job_dir(pos, {'ENCUT': 500, 'IBRION': 2}, str(out),
                              lib_root=mini_lib)
    txt = (out / 'INCAR').read_text(encoding='utf-8')
    assert 'ENCUT = 500' in txt
    assert 'IBRION = 2' in txt
