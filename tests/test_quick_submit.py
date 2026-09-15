"""quick_submit 测试:引擎识别全格式 / build_quick_jobs 建目录+manifest+重名后缀+skipped /
submit_hint 文案。全部本地文件级,离线可测。
"""
import os
from pathlib import Path

import pytest

from vcstudio.cluster import quick_submit
from vcstudio.shared import manifest as manifest_mod

GJF_WITH_CHK = ('%chk=water_opt.chk\n#P PBE def2-SVP sp\n\n'
                'a title line\n\n0 1\nO 0.0 0.0 0.0\n\n')
GJF_TITLE_ONLY = ('#P PBE def2-SVP opt\n\nWater Optimization\n\n0 1\nO 0.0 0.0 0.0\n\n')
POSCAR_FE = ('Fe slab\n1.0\n8 0 0\n0 8 0\n0 0 15\nFe\n1\nDirect\n0 0 0\n')
POSCAR_O = ('O box\n1.0\n12 0 0\n0 12 0\n0 0 12\nO\n1\nDirect\n0 0 0\n')
KPOINTS_GAMMA = 'Automatic\n0\nGamma\n1 1 1\n0 0 0\n'
POTCAR_FE = 'TITEL = PAW_PBE Fe\n'


def _write(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def _potlib(root, entries=(('Fe', 300), ('O', 400))):
    for variant, enmax in entries:
        _write(str(root / variant / 'POTCAR'),
               f'TITEL = PAW_PBE {variant}\nENMAX = {enmax}; ENMIN = 200\n')
    return str(root)


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
    assert out['ok'] is False
    assert out['jobs'] == [] and len(out['skipped']) == 1
    assert '无法识别' in out['skipped'][0]['reason']
    assert '未生成任何作业' in out['error']


def test_build_skips_missing_file(tmp_path):
    out = quick_submit.build_quick_jobs([str(tmp_path / 'ghost.gjf')], str(tmp_path / 'out'))
    assert out['ok'] is False
    assert out['jobs'] == [] and '不存在' in out['skipped'][0]['reason']


def test_build_vasp_directory_copies_input_set(tmp_path):
    d = tmp_path / 'vaspjob'
    _write(str(d / 'INCAR'), 'ENCUT=400\nNSW=0\n')
    _write(str(d / 'POSCAR'), POSCAR_FE)
    _write(str(d / 'KPOINTS'), KPOINTS_GAMMA)
    _write(str(d / 'POTCAR'), POTCAR_FE)
    out = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))
    job = out['jobs'][0]
    assert job['engine'] == 'vasp' and job['name'] == 'vaspjob'
    assert set(job['files']) == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    for fn in ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'):
        assert os.path.isfile(os.path.join(job['dir'], fn))
    assert manifest_mod.load_manifest(job['dir'])['task_type'] == 'static'


def test_build_vasp_directory_skips_when_quartet_is_incomplete(tmp_path):
    d = tmp_path / 'vaspjob'
    _write(str(d / 'INCAR'), 'ENCUT=400\nNSW=0\n')
    _write(str(d / 'POSCAR'), 'Fe slab\n')
    _write(str(d / 'KPOINTS'), 'auto\n')

    out = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))

    assert out['ok'] is False
    assert out['jobs'] == []
    assert len(out['skipped']) == 1
    assert 'POTCAR' in out['skipped'][0]['reason']
    assert 'POTCAR' in out['error']
    assert not (tmp_path / 'out' / 'vaspjob').exists()


def test_build_vasp_directory_blocks_invalid_complete_quartet(tmp_path):
    d = tmp_path / 'bad-quartet'
    _write(str(d / 'INCAR'), 'ENCUT=400\nNSW=0\n')
    _write(str(d / 'POSCAR'), POSCAR_FE)
    _write(str(d / 'KPOINTS'), 'Automatic\n0\nGamma\n0 1 1\n')
    _write(str(d / 'POTCAR'), POTCAR_FE)

    scanned = quick_submit.scan_inputs([str(d)])
    built = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))

    assert scanned['items'][0]['status'] == 'invalid'
    assert scanned['items'][0]['can_build'] is False
    assert '正整数' in scanned['items'][0]['message']
    assert built['ok'] is False and built['jobs'] == []
    assert not (tmp_path / 'out' / 'bad-quartet').exists()


@pytest.mark.parametrize(
    'kpoints',
    [
        'Band path\n20\nLine-mode\nReciprocal\n0 0 nan\n0.5 0 0\n',
        'Explicit\n2\nReciprocal\n0 0 0 1\n0.5 garbage 0 1\n',
    ],
)
def test_scan_vasp_directory_blocks_bad_line_or_explicit_kpoints(tmp_path, kpoints):
    d = tmp_path / 'bad-kpoints'
    _write(str(d / 'INCAR'), 'ENCUT=400\nNSW=0\n')
    _write(str(d / 'POSCAR'), POSCAR_FE)
    _write(str(d / 'KPOINTS'), kpoints)
    _write(str(d / 'POTCAR'), POTCAR_FE)

    scanned = quick_submit.scan_inputs([str(d)])

    assert scanned['items'][0]['status'] == 'invalid'
    assert scanned['items'][0]['can_build'] is False
    assert ('有限数值' in scanned['items'][0]['message']
            or '非数值' in scanned['items'][0]['message'])


def test_build_vasp_directory_accepts_negative_poscar_volume_scale(tmp_path):
    d = tmp_path / 'negative-scale'
    negative = POSCAR_FE.replace('\n1.0\n', '\n-960.0\n', 1)
    _write(str(d / 'INCAR'), 'ENCUT=400\nNSW=0\n')
    _write(str(d / 'POSCAR'), negative)
    _write(str(d / 'KPOINTS'), KPOINTS_GAMMA)
    _write(str(d / 'POTCAR'), POTCAR_FE)

    scanned = quick_submit.scan_inputs([str(d)])
    built = quick_submit.build_quick_jobs([str(d)], str(tmp_path / 'out'))

    assert scanned['items'][0]['status'] == 'ready'
    assert built['ok'] is True and len(built['jobs']) == 1
    assert Path(built['jobs'][0]['dir'], 'POSCAR').read_text(encoding='utf-8') == negative


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
    _write(str(d / 'INCAR'), incar_text)
    _write(str(d / 'POSCAR'), POSCAR_FE)
    _write(str(d / 'KPOINTS'), KPOINTS_GAMMA)
    _write(str(d / 'POTCAR'), POTCAR_FE)

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


# ── 父目录预检 + POSCAR/CONTCAR 自动补齐四件套 ───────────────────────────────
def test_scan_parent_recursively_finds_jobs_and_reports_missing(tmp_path):
    ready = tmp_path / 'batch' / 'ready'
    for name, text in (
        ('INCAR', 'ENCUT=500\n'), ('POSCAR', POSCAR_FE),
        ('KPOINTS', 'Automatic\n0\nGamma\n1 1 1\n0 0 0\n'),
        ('POTCAR', 'TITEL = PAW_PBE Fe\n'),
    ):
        _write(str(ready / name), text)
    structure = tmp_path / 'batch' / 'only_structure'
    _write(str(structure / 'CONTCAR'), POSCAR_O)
    shared = _write(str(tmp_path / 'shared' / 'INCAR'), 'EDIFF=1E-5\nNSW=0\n')

    out = quick_submit.scan_inputs([str(tmp_path / 'batch')], shared_incar=shared)

    assert out['ok'] is True
    assert out['summary'] == {
        'total': 2, 'ready': 1, 'generatable': 1, 'blocked': 0,
        'duplicates': 0, 'shared_incar_valid': True,
    }
    by_name = {item['name']: item for item in out['items']}
    assert by_name['ready']['mode'] == 'copy'
    assert by_name['only_structure']['mode'] == 'generate'
    assert set(by_name['only_structure']['missing']) == set(quick_submit._VASP_INPUTS)


def test_scan_parent_and_child_deduplicate_and_do_not_follow_symlink(tmp_path):
    job = tmp_path / 'root' / 'job'
    _write(str(job / 'POSCAR'), POSCAR_FE)
    link = tmp_path / 'root' / 'loop'
    try:
        os.symlink(tmp_path / 'root', link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('当前文件系统不支持目录符号链接')

    out = quick_submit.scan_inputs([str(job), str(tmp_path / 'root'), str(job)])

    assert len(out['items']) == 1
    assert out['items'][0]['path'] == str(job)
    assert out['summary']['duplicates'] >= 2


def test_scan_structure_without_shared_incar_is_actionable_blocked(tmp_path):
    source = tmp_path / 'structure'
    _write(str(source / 'POSCAR'), POSCAR_FE)

    out = quick_submit.scan_inputs([str(source)])

    item = out['items'][0]
    assert item['status'] == 'incomplete' and item['can_build'] is False
    assert '共享 INCAR' in item['message']
    assert item['missing'] == ['INCAR', 'KPOINTS', 'POTCAR']


def test_scan_local_incar_symlink_is_blocked_even_with_shared_fallback(tmp_path):
    source = tmp_path / 'structure'
    _write(str(source / 'POSCAR'), POSCAR_FE)
    target = _write(str(tmp_path / 'target' / 'INCAR'), 'ENCUT=450\n')
    shared = _write(str(tmp_path / 'shared' / 'INCAR'), 'ENCUT=500\n')
    try:
        os.symlink(target, source / 'INCAR')
    except (OSError, NotImplementedError):
        pytest.skip('当前文件系统不支持文件符号链接')

    out = quick_submit.scan_inputs([str(source)], shared_incar=shared)

    item = out['items'][0]
    assert item['status'] == 'invalid' and item['can_build'] is False
    assert '符号链接' in item['message']


@pytest.mark.parametrize('text,expected', [
    ('# comments only\n', 'KEY=VALUE'),
    ('ENCUT=abc\n', 'ENCUT'),
    ('ENCUT=500\nISPIN=3\n', 'ISPIN'),
    ('ENCUT=500\nNSW=ten\n', 'NSW'),
])
def test_scan_invalid_local_incar_is_not_masked_by_shared(tmp_path, text, expected):
    source = tmp_path / expected
    _write(str(source / 'POSCAR'), POSCAR_FE)
    _write(str(source / 'INCAR'), text)
    shared = _write(str(tmp_path / 'shared' / 'INCAR'), 'ENCUT=500\nISPIN=2\n')

    item = quick_submit.scan_inputs([str(source)], shared_incar=shared)['items'][0]

    assert item['status'] == 'invalid' and item['can_build'] is False
    assert expected in item['message']


def test_scan_structure_with_local_incar_is_generatable_without_shared(tmp_path):
    source = tmp_path / 'structure'
    _write(str(source / 'POSCAR'), POSCAR_FE)
    local = _write(str(source / 'INCAR'), 'NSW=8\nIBRION=2\n')

    out = quick_submit.scan_inputs([str(source)])

    item = out['items'][0]
    assert item['status'] == 'generatable' and item['can_build'] is True
    assert item['source_incar'] == local
    assert item['source_incar_origin'] == 'local'
    assert item['missing'] == ['KPOINTS', 'POTCAR']
    assert '本目录 INCAR' in item['message']


def test_scan_ambiguous_local_incars_does_not_fall_back_to_shared(tmp_path):
    source = tmp_path / 'structure'
    _write(str(source / 'POSCAR'), POSCAR_FE)
    _write(str(source / 'INCAR'), 'NSW=8\n')
    _write(str(source / 'incar'), 'NSW=0\n')
    if (source / 'INCAR').samefile(source / 'incar'):
        pytest.skip('filesystem is case-insensitive and cannot represent this conflict')
    shared = _write(str(tmp_path / 'shared' / 'INCAR'), 'NSW=0\n')

    out = quick_submit.scan_inputs([str(source)], shared_incar=shared)

    item = out['items'][0]
    assert item['status'] == 'invalid' and item['can_build'] is False
    assert item['source_incar'] == '' and item['source_incar_origin'] == ''
    assert '多个可读 INCAR' in item['message']


def test_scan_does_not_count_shared_incar_parent_as_a_job(tmp_path):
    root = tmp_path / 'batch'
    shared = _write(str(root / 'INCAR'), 'ENCUT=500\n')
    _write(str(root / 'a' / 'POSCAR'), POSCAR_FE)
    _write(str(root / 'b' / 'POSCAR'), POSCAR_O)

    out = quick_submit.scan_inputs([str(root)], shared_incar=shared)

    assert out['summary']['total'] == 2
    assert {item['name'] for item in out['items']} == {'a', 'b'}
    assert all(item['status'] == 'generatable' for item in out['items'])


def test_build_structure_with_shared_incar_generates_quartet_without_touching_source(tmp_path):
    source = tmp_path / 'source' / 'fe'
    poscar = _write(str(source / 'POSCAR'), POSCAR_FE)
    shared = _write(str(tmp_path / 'shared' / 'INCAR'), 'EDIFF=1E-6\nNSW=0\n')
    lib = _potlib(tmp_path / 'potpaw')
    before = open(poscar, 'rb').read()

    out = quick_submit.build_quick_jobs(
        [str(source)], str(tmp_path / 'jobs'), shared_incar=shared, lib_root=lib)

    assert out['ok'] is True and len(out['jobs']) == 1
    job = out['jobs'][0]
    assert job['mode'] == 'generate'
    assert set(job['files']) == set(quick_submit._VASP_INPUTS)
    assert open(poscar, 'rb').read() == before
    assert sorted(p.name for p in source.iterdir()) == ['POSCAR']
    incar_out = open(os.path.join(job['dir'], 'INCAR'), encoding='utf-8').read()
    assert 'ENCUT = 400' in incar_out
    manifest = manifest_mod.load_manifest(job['dir'])
    assert manifest['task_type'] == 'static'
    assert manifest['inputs']['quick_submit_mode'] == 'generate'
    assert manifest['inputs']['source_structure'] == poscar
    assert manifest['inputs']['source_incar'] == shared
    assert manifest['inputs']['source_incar_origin'] == 'shared'
    assert manifest['inputs']['shared_incar'] == shared


def test_build_local_incar_wins_over_shared_and_is_recorded(tmp_path):
    source = tmp_path / 'source' / 'fe'
    _write(str(source / 'POSCAR'), POSCAR_FE)
    local = _write(
        str(source / 'INCAR'), 'SYSTEM=local-member\nENCUT=400\nNSW=8\nIBRION=2\n')
    shared = _write(
        str(tmp_path / 'shared' / 'INCAR'), 'SYSTEM=shared-template\nENCUT=400\nNSW=0\n')
    lib = _potlib(tmp_path / 'potpaw')

    out = quick_submit.build_quick_jobs(
        [str(source)], str(tmp_path / 'jobs'), shared_incar=shared, lib_root=lib)

    assert out['ok'] is True and not out['skipped']
    job = out['jobs'][0]
    incar_out = Path(job['dir'], 'INCAR').read_text(encoding='utf-8')
    assert 'SYSTEM=local-member' in incar_out
    assert 'SYSTEM=shared-template' not in incar_out
    manifest = manifest_mod.load_manifest(job['dir'])
    assert manifest['task_type'] == 'relax'
    assert manifest['inputs']['source_incar'] == local
    assert manifest['inputs']['source_incar_origin'] == 'local'
    assert manifest['inputs']['source_incar_sha256'] == manifest_mod.sha256_file(local)
    assert 'shared_incar' not in manifest['inputs']


def test_generated_batch_uses_one_group_encut(tmp_path):
    fe = tmp_path / 'source' / 'fe'
    oxygen = tmp_path / 'source' / 'oxygen'
    _write(str(fe / 'POSCAR'), POSCAR_FE)
    _write(str(oxygen / 'POSCAR'), POSCAR_O)
    shared = _write(str(tmp_path / 'shared' / 'INCAR'), 'EDIFF=1E-5\n')
    lib = _potlib(tmp_path / 'potpaw')

    out = quick_submit.build_quick_jobs(
        [str(tmp_path / 'source')], str(tmp_path / 'jobs'),
        shared_incar=shared, lib_root=lib)

    assert len(out['jobs']) == 2 and not out['skipped']
    incars = [open(os.path.join(job['dir'], 'INCAR'), encoding='utf-8').read()
              for job in out['jobs']]
    # 1.3 × max(Fe=300,O=400) 向上取整到 50 eV -> 全组均为 550 eV。
    assert all('ENCUT = 550' in text for text in incars)


def test_generation_failure_is_isolated_from_valid_sibling(tmp_path):
    good = tmp_path / 'source' / 'good'
    bad = tmp_path / 'source' / 'bad'
    _write(str(good / 'POSCAR'), POSCAR_FE)
    _write(str(bad / 'POSCAR'), 'not a POSCAR\n')
    shared = _write(str(tmp_path / 'shared' / 'INCAR'), 'NSW=0\n')
    lib = _potlib(tmp_path / 'potpaw')

    out = quick_submit.build_quick_jobs(
        [str(tmp_path / 'source')], str(tmp_path / 'jobs'),
        shared_incar=shared, lib_root=lib)

    assert len(out['jobs']) == 1 and out['jobs'][0]['name'] == 'good'
    assert len(out['skipped']) == 1 and out['skipped'][0]['file'] == str(bad)
    assert out['skipped'][0]['status'] == 'generation_failed'
    assert not list((tmp_path / 'jobs').glob('.*.building-*'))


def test_complete_quartet_still_imports_when_shared_incar_path_is_invalid(tmp_path):
    source = tmp_path / 'ready'
    for name, text in (
        ('INCAR', 'ENCUT=500\nNSW=0\n'), ('POSCAR', POSCAR_FE),
        ('KPOINTS', 'Automatic\n0\nGamma\n1 1 1\n0 0 0\n'),
        ('POTCAR', 'TITEL = PAW_PBE Fe\n'),
    ):
        _write(str(source / name), text)

    out = quick_submit.build_quick_jobs(
        [str(source)], str(tmp_path / 'jobs'), shared_incar='/missing/INCAR')

    assert len(out['jobs']) == 1 and out['jobs'][0]['mode'] == 'copy'
    assert out['skipped'] == []


def test_build_refuses_output_inside_source_calculation_directory(tmp_path):
    source = tmp_path / 'source'
    for name, text in (
        ('INCAR', 'ENCUT=500\n'), ('POSCAR', POSCAR_FE),
        ('KPOINTS', 'Automatic\n0\nGamma\n1 1 1\n0 0 0\n'),
        ('POTCAR', 'TITEL = PAW_PBE Fe\n'),
    ):
        _write(str(source / name), text)

    out = quick_submit.build_quick_jobs([str(source)], str(source / 'generated'))

    assert out['ok'] is False
    assert '所选源目录内部' in out['error']
    assert out['jobs'] == []
    assert out['skipped'][0]['status'] == 'unsafe_output_root'
    assert sorted(p.name for p in source.iterdir()) == ['INCAR', 'KPOINTS', 'POSCAR', 'POTCAR']


def test_build_refuses_output_inside_selected_parent_not_only_candidate(tmp_path):
    parent = tmp_path / 'batch'
    source = parent / 'job_a'
    for name, text in (
        ('INCAR', 'ENCUT=500\n'), ('POSCAR', POSCAR_FE),
        ('KPOINTS', 'Automatic\n0\nGamma\n1 1 1\n0 0 0\n'),
        ('POTCAR', 'TITEL = PAW_PBE Fe\n'),
    ):
        _write(str(source / name), text)

    out = quick_submit.build_quick_jobs([str(parent)], str(parent / 'generated'))

    assert out['ok'] is False
    assert out['jobs'] == []
    assert out['skipped'][0]['file'] == str(parent)
    assert out['skipped'][0]['status'] == 'unsafe_output_root'
    assert not (parent / 'generated').exists()


def test_quick_submit_ui_exposes_guided_scan_and_generation_controls():
    root = Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' / 'assets'
    html = (root / 'index.html').read_text(encoding='utf-8')
    for control_id in (
        'qs-add-dir', 'qs-incar', 'qs-incar-btn', 'qs-lib', 'qs-calc-type',
        'qs-preview', 'qs-build', 'qs-next', 'qs-go-submit',
    ):
        assert f'id="{control_id}"' in html
    assert '选择父目录并扫描' in html
    assert '源目录不会被修改或覆盖' in html


def test_quick_submit_ui_scans_automatically_and_selects_new_jobs():
    jobs_js = (Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' /
               'assets' / 'jobs.js').read_text(encoding='utf-8')
    assert "VCS.call('quick_submit_scan', QS.files, qsSharedIncar())" in jobs_js
    assert "'quick_submit_build', QS.files, out, '', shared, lib, calcType" in jobs_js
    assert 'State.selected.add(job.dir)' in jobs_js
    assert "wire('qs-go-submit', doSubmit)" in jobs_js
    assert "next.scrollIntoView({ behavior: 'smooth', block: 'center' })" in jobs_js
    assert "card.scrollIntoView({ behavior: 'smooth', block: 'start' })" not in jobs_js
