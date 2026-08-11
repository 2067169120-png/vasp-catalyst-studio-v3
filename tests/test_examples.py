"""examples/quickstart 端到端:假库生成脚本 + build_job_dir 出四件套。"""
import os
import subprocess
import sys

from vcstudio.generate.job_builder import build_job_dir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, 'examples', 'quickstart')


def test_demo_potcar_lib_script_builds_fake_library(tmp_path):
    lib = tmp_path / 'demo_lib'
    r = subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join(ROOT, 'examples', 'make_demo_potcar_lib.py'),
         str(lib)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (lib / 'C' / 'POTCAR').read_text(encoding='utf-8')
    assert 'fake PAW_PBE C' in text and 'ENMAX' in text


def test_quickstart_example_generates_four_files(tmp_path):
    lib = tmp_path / 'demo_lib'
    subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join(ROOT, 'examples', 'make_demo_potcar_lib.py'),
         str(lib)], check=True)
    out = tmp_path / 'job'
    res = build_job_dir(os.path.join(EX, 'POSCAR'), os.path.join(EX, 'INCAR'),
                        str(out), calc_type='slab', lib_root=str(lib))
    assert res['ok']
    for name in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'):
        assert (out / name).is_file(), name
    # 二维 slab → kz=1
    assert res['kpoints'][2] == 1
