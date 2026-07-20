"""quick_submit 测试:引擎识别全格式 / build_quick_jobs 建目录+manifest+重名后缀+skipped /
submit_hint 文案。全部本地文件级,离线可测。
"""
import os

import pytest

from vcstudio.cluster import quick_submit
from vcstudio.shared import manifest as manifest_mod

GJF_WITH_CHK = ('%chk=water_opt.chk\n#P PBE def2-SVP sp\n\n'
                'a title line\n\n0 1\nO 0.0 0.0 0.0\n\n')
GJF_TITLE_ONLY = ('#P PBE def2-SVP opt\n\nWater Optimization\n\n0 1\nO 0.0 0.0 0.0\n\n')
VASP_POSCAR = ('Fe slab\n1.0\n10 0 0\n0 10 0\n0 0 15\nFe\n1\nDirect\n'
                '0.0 0.0 0.5\n')
VASP_KPOINTS = 'Gamma mesh\n0\nGamma\n3 3 1\n0 0 0\n'
VASP_POTCAR = ('TITEL  = PAW_PBE Fe 06Sep2000\n'
                'ENMAX  =  400.000; ENMIN = 300.000 eV\n')


def _write(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def _write_vasp_quartet(directory, incar='ENCUT=400\nNSW=0\n'):
    """写一套能通过共享导入门控的最小、真实格式 VASP 输入。"""
    _write(str(directory / 'INCAR'), incar)
    _write(str(directory / 'POSCAR'), VASP_POSCAR)
    _write(str(directory / 'KPOINTS'), VASP_KPOINTS)
    _write(str(directory / 'POTCAR'), VASP_POTCAR)


# ── detect_engine ─────────────────────────────────────────────────────────────
def test_detect_gaussian_gjf():
    assert quick_submit.detect_engine('run.gjf') == 'gaussian'


def test_detect_gaussian_com():
    assert quick_submit.detect_engine('/x/run.com') == 'gaussian'


def test_detect_cp2k_inp():
    assert quick_submit.detect_engine('job.inp') == 'cp2k'


def test_detect_castep_cell():
    assert quick_submit.detect_engine('seed.cell') == 'castep'


def test_detect_castep_param():
    assert quick_submit.detect_engine('seed.param') == 'castep'


def test_detect_vasp_incar_filename():
    assert quick_submit.detect_engine('/job/INCAR') == 'vasp'


def test_detect_vasp_poscar_filename():
    assert quick_submit.detect_engine('POSCAR') == 'vasp'


def test_detect_vasp_directory(tmp_path):
    _write(str(tmp_path / 'INCAR'), 'ENCUT=400\n')
    _write(str(tmp_path / 'POSCAR'), 'x\n')
    assert quick_submit.detect_engine(str(tmp_path)) == 'vasp'


def test_detect_unknown_file_none():
    assert quick_submit.detect_engine('notes.txt') is None


def test_detect_empty_directory_none(tmp_path):
    assert quick_submit.detect_engine(str(tmp_path)) is None


def test_detect_case_insensitive_extension():
    assert quick_submit.detect_engine('RUN.GJF') == 'gaussian'


# ── submit_hint ───────────────────────────────────────────────────────────────
def test_submit_hint_gaussian():
    assert 'g16' in quick_submit.submit_hint('gaussian')


def test_submit_hint_all_engines_nonempty():
    for eng in ('gaussian', 'cp2k', 'castep', 'vasp'):
        assert quick_submit.submit_hint(eng)


def test_submit_hint_unknown_empty():
    assert quick_submit.submit_hint('orca') == ''


# ── build_quick_jobs ──────────────────────────────────────────────────────────
def test_build_creates_dir_and_manifest(tmp_path):
    src = _write(str(tmp_path / 'in' / 'run.gjf'), GJF_WITH_CHK)
    out = quick_submit.build_quick_jobs([src], str(tmp_path / 'out'))
    assert out['ok'] is True and len(out['jobs']) == 1
    job = out['jobs'][0]
    assert job['engine'] == 'gaussian' and os.path.isdir(job['dir'])
    m = manifest_mod.load_manifest(job['dir'])
    assert m['task_type'] == 'quick' and m['state'] == 'CREATED'
    assert m['inputs']['engine'] == 'gaussian'


def test_build_copies_file_and_records_sha256(tmp_path):
    src = _write(str(tmp_path / 'in' / 'run.gjf'), GJF_WITH_CHK)
    out = quick_submit.build_quick_jobs([src], str(tmp_path / 'out'))
    job = out['jobs'][0]
    copied = os.path.join(job['dir'], 'run.gjf')
    assert os.path.isfile(copied)
    m = manifest_mod.load_manifest(job['dir'])
    assert m['inputs']['sha256']['run.gjf'] == manifest_mod.sha256_file(copied)


def test_build_gjf_name_from_chk(tmp_path):
    src = _write(str(tmp_path / 'in' / 'anything.gjf'), GJF_WITH_CHK)
    out = quick_submit.build_quick_jobs([src], str(tmp_path / 'out'))
    assert out['jobs'][0]['name'] == 'water_opt'


def test_build_gjf_name_from_title(tmp_path):
    src = _write(str(tmp_path / 'in' / 'anything.gjf'), GJF_TITLE_ONLY)
    out = quick_submit.build_quick_jobs([src], str(tmp_path / 'out'))
    assert out['jobs'][0]['name'] == 'Water_Optimization'


def test_build_name_collision_gets_suffix(tmp_path):
    a = _write(str(tmp_path / 'a' / 'x.gjf'), GJF_WITH_CHK)   # both → 'water_opt'
    b = _write(str(tmp_path / 'b' / 'y.gjf'), GJF_WITH_CHK)
    out = quick_submit.build_quick_jobs([a, b], str(tmp_path / 'out'))
    names = [j['name'] for j in out['jobs']]
    assert names == ['water_opt', 'water_opt_2']
    assert all(os.path.isdir(j['dir']) for j in out['jobs'])


def test_build_skips_unknown_engine(tmp_path):
    src = _write(str(tmp_path / 'notes.txt'), 'hello')
    out = quick_submit.build_quick_jobs([src], str(tmp_path / 'out'))
    assert out['jobs'] == [] and len(out['skipped']) == 1
    assert '无法识别' in out['skipped'][0]['reason']


def test_build_skips_missing_file(tmp_path):
    out = quick_submit.build_quick_jobs([str(tmp_path / 'ghost.gjf')], str(tmp_path / 'out'))
    assert out['jobs'] == [] and '不存在' in out['skipped'][0]['reason']


def test_build_vasp_directory_copies_input_set(tmp_path):
    d = tmp_path / 'vaspjob'
    _write_vasp_quartet(d)
    out = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))
    job = out['jobs'][0]
    assert job['engine'] == 'vasp' and job['name'] == 'vaspjob'
    assert set(job['files']) == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    for fn in ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'):
        assert os.path.isfile(os.path.join(job['dir'], fn))
    saved = manifest_mod.load_manifest(job['dir'])
    assert saved['task_type'] == 'static'
    assert saved['inputs']['elements'] == ['Fe']
    assert saved['inputs']['counts'] == [1]
    assert saved['inputs']['import_input_issues'] == []


def test_build_vasp_directory_skips_when_quartet_is_incomplete(tmp_path):
    d = tmp_path / 'vaspjob'
    _write(str(d / 'INCAR'), 'ENCUT=400\nNSW=0\n')
    _write(str(d / 'POSCAR'), VASP_POSCAR)
    _write(str(d / 'KPOINTS'), VASP_KPOINTS)

    out = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))

    assert out['ok'] is True
    assert out['jobs'] == []
    assert len(out['skipped']) == 1
    assert 'POTCAR' in out['skipped'][0]['reason']
    assert not (tmp_path / 'out' / 'vaspjob').exists()


def test_build_vasp_directory_skips_malformed_complete_quartet(tmp_path):
    """四个文件都在也不够：坏 KPOINTS/空文件等必须在建作业前拒绝。"""
    d = tmp_path / 'bad-vasp'
    _write_vasp_quartet(d)
    _write(str(d / 'KPOINTS'), 'bad mesh\n0\nGamma\n3 0 1\n')

    out = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))

    assert out['ok'] is True and out['jobs'] == []
    assert len(out['skipped']) == 1
    assert '输入门控未通过' in out['skipped'][0]['reason']
    assert '正整数' in out['skipped'][0]['reason']
    assert not (tmp_path / 'out' / 'bad-vasp').exists()


@pytest.mark.parametrize(
    ('incar_text', 'expected'),
    [
        ('ENCUT = 500\nNSW = 0\n', 'static'),
        ('ENCUT = 500\nNSW = 200\nIBRION = 2\n', 'relax'),
        ('ENCUT = 500\nNSW = 1\nIBRION = 5\n', 'freq'),
    ],
)
def test_build_vasp_directory_infers_task_type_from_incar(tmp_path, incar_text, expected):
    d = tmp_path / expected
    _write_vasp_quartet(d, incar=incar_text)

    out = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))

    assert out['ok'] is True and len(out['jobs']) == 1
    assert manifest_mod.load_manifest(out['jobs'][0]['dir'])['task_type'] == expected


def test_build_job_prefix_applied(tmp_path):
    src = _write(str(tmp_path / 'in' / 'run.gjf'), GJF_WITH_CHK)
    out = quick_submit.build_quick_jobs([src], str(tmp_path / 'out'), job_prefix='batch_')
    assert out['jobs'][0]['name'] == 'batch_water_opt'


def test_build_mixed_batch_partial_skip(tmp_path):
    good = _write(str(tmp_path / 'in' / 'run.inp'), 'x')
    bad = _write(str(tmp_path / 'in' / 'readme.md'), 'x')
    out = quick_submit.build_quick_jobs([good, bad], str(tmp_path / 'out'))
    assert len(out['jobs']) == 1 and out['jobs'][0]['engine'] == 'cp2k'
    assert len(out['skipped']) == 1 and out['skipped'][0]['file'] == bad


def test_build_manifest_has_submit_hint_warning(tmp_path):
    src = _write(str(tmp_path / 'in' / 'run.gjf'), GJF_WITH_CHK)
    out = quick_submit.build_quick_jobs([src], str(tmp_path / 'out'))
    m = manifest_mod.load_manifest(out['jobs'][0]['dir'])
    assert any('g16' in w for w in m['warnings'])
