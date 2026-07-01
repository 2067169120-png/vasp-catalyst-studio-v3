"""cli/main.py:vcs gen 子命令(成功 0 / 错误非零 + stderr)。"""
from vcstudio.cli.main import main

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

POSCAR_VASP4 = """title
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
2 4
Direct
"""

USER_INCAR = "ENCUT = 500\nIBRION = 2\n"


def _setup(tmp_path, poscar_text):
    pos = tmp_path / 'POSCAR'
    pos.write_text(poscar_text, encoding='utf-8')
    inc = tmp_path / 'user.incar'
    inc.write_text(USER_INCAR, encoding='utf-8')
    return str(pos), str(inc)


def test_gen_success(tmp_path, mini_lib):
    pos, inc = _setup(tmp_path, POSCAR_CN)
    out = tmp_path / 'job'
    rc = main(['gen', '--poscar', pos, '--incar', inc, '-o', str(out),
               '--lib-root', mini_lib])
    assert rc == 0
    for name in ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR'):
        assert (out / name).is_file()


def test_gen_kpoints_flag(tmp_path, mini_lib):
    pos, inc = _setup(tmp_path, POSCAR_CN)
    out = tmp_path / 'job'
    rc = main(['gen', '--poscar', pos, '--incar', inc, '-o', str(out),
               '--kpoints', '5 5 1', '--lib-root', mini_lib])
    assert rc == 0
    assert '5 5 1' in (out / 'KPOINTS').read_text(encoding='utf-8')


def test_gen_missing_poscar_nonzero(tmp_path, mini_lib, capsys):
    _, inc = _setup(tmp_path, POSCAR_CN)
    rc = main(['gen', '--poscar', str(tmp_path / 'nope'), '--incar', inc,
               '-o', str(tmp_path / 'j'), '--lib-root', mini_lib])
    assert rc == 1
    assert '错误' in capsys.readouterr().err


def test_gen_vasp4_nonzero(tmp_path, mini_lib, capsys):
    pos, inc = _setup(tmp_path, POSCAR_VASP4)
    rc = main(['gen', '--poscar', pos, '--incar', inc, '-o', str(tmp_path / 'j'),
               '--lib-root', mini_lib])
    assert rc == 1
    assert '错误' in capsys.readouterr().err


def test_no_command_returns_nonzero():
    assert main([]) == 2
