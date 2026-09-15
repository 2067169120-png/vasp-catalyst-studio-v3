import pytest

from vcstudio.gui.logic import (
    parse_kpoints_field, validate_generate_inputs,
    profile_from_form, validate_cluster_inputs,
)
from vcstudio.cluster.profiles import ClusterProfile


def test_parse_kpoints_auto_and_empty():
    assert parse_kpoints_field('') is None
    assert parse_kpoints_field('  ') is None
    assert parse_kpoints_field('自动') is None
    assert parse_kpoints_field('auto') is None


def test_parse_kpoints_valid():
    assert parse_kpoints_field('5 5 1') == [5, 5, 1]


def test_parse_kpoints_bad_raises():
    with pytest.raises(ValueError):
        parse_kpoints_field('5 5')
    with pytest.raises(ValueError):
        parse_kpoints_field('a b c')


def test_validate_generate_inputs_collects_errors(tmp_path):
    errs = validate_generate_inputs('', '', '', '')
    assert any('赝势库' in e for e in errs)
    assert any('POSCAR' in e for e in errs)
    assert any('INCAR' in e for e in errs)
    assert any('输出' in e for e in errs)


def test_validate_generate_inputs_ok(tmp_path):
    p = tmp_path / 'POSCAR'
    p.write_text('x')
    i = tmp_path / 'INCAR'
    i.write_text('x')
    assert validate_generate_inputs(str(p), str(i), str(tmp_path / 'out'), 'D:/lib') == []


def test_profile_from_form_maps_fields():
    prof = profile_from_form('bj', {
        'hostname': 'h', 'port': '22', 'username': 'u', 'auth': 'key',
        'key_path': '/k', 'use_jump': False, 'jump_host': '', 'jump_user': '',
        'jump_port': '22', 'remote_root': '/r', 'scheduler': 'Slurm'})
    assert isinstance(prof, ClusterProfile)
    assert prof.name == 'bj' and prof.port == 22 and prof.remote_root == '/r'


def test_validate_cluster_inputs():
    ok = ClusterProfile(name='c', hostname='h', username='u', auth='key', key_path='/k')
    assert validate_cluster_inputs(ok, has_password=False) == []
    bad = ClusterProfile(name='', hostname='', username='', auth='password')
    errs = validate_cluster_inputs(bad, has_password=False)
    assert any('主机' in e for e in errs)
    assert any('密码' in e for e in errs)
