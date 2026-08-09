"""molbuild.external_editor 测试:导出→mtime→回读 往返(真实临时文件)+ open_with 假 subprocess。"""
import os

import pytest

from vcstudio.molbuild import external_editor as ee

_ELS = ['O', 'H', 'H']
_XYZ = [[0.1, 0.2, 0.3], [0.96, 0.0, 0.0], [-0.24, 0.93, 0.0]]


def test_export_xyz_writes_fixed_name(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))
    assert r['ok'] and r['error'] == ''
    assert os.path.basename(r['path']) == 'vcstudio_external_edit.xyz'
    assert os.path.isfile(r['path'])
    assert isinstance(r['mtime'], float)


def test_export_mol_writes_fixed_name(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'mol', workdir=str(tmp_path))
    assert r['ok']
    assert os.path.basename(r['path']) == 'vcstudio_external_edit.mol'


def test_export_bad_format_and_length_mismatch(tmp_path):
    bad = ee.export_for_editor(_ELS, _XYZ, 'pdb', workdir=str(tmp_path))
    assert not bad['ok'] and 'xyz/mol' in bad['error']
    mism = ee.export_for_editor(['O', 'H'], _XYZ, 'xyz', workdir=str(tmp_path))
    assert not mism['ok'] and '不一致' in mism['error']


def test_roundtrip_xyz(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))
    back = ee.reimport(r['path'])
    assert back['ok']
    assert back['elements'] == _ELS
    for got, want in zip(back['coords'], _XYZ):
        assert got == pytest.approx(want, abs=1e-6)


def test_roundtrip_mol(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'mol', workdir=str(tmp_path))
    back = ee.reimport(r['path'])
    assert back['ok']
    assert back['elements'] == _ELS
    for got, want in zip(back['coords'], _XYZ):
        assert got == pytest.approx(want, abs=1e-4)     # MOL V2000 为 4 位小数


def test_check_reimport_unchanged_then_changed(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))
    assert ee.check_reimport(r['path'], r['mtime'])['changed'] is False
    os.utime(r['path'], (r['mtime'] + 10, r['mtime'] + 10))    # 模拟用户改存
    c = ee.check_reimport(r['path'], r['mtime'])
    assert c['changed'] is True and c['mtime'] > r['mtime']


def test_check_reimport_missing_and_none_baseline(tmp_path):
    missing = ee.check_reimport(str(tmp_path / 'nope.xyz'), 123.0)
    assert missing['changed'] is False and missing['mtime'] is None
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))
    # last_mtime=None(从未记录)且文件在 → 视作已变化
    assert ee.check_reimport(r['path'], None)['changed'] is True


def test_reimport_missing_file(tmp_path):
    out = ee.reimport(str(tmp_path / 'nope.xyz'))
    assert not out['ok'] and '读取' in out['error']


def test_reimport_normalises_symbol_and_numeric_z(tmp_path):
    p = tmp_path / 't.xyz'
    p.write_text('2\ncomment\nFE 0.0 0.0 0.0\n8 1.0 1.0 1.0\n', encoding='utf-8')
    out = ee.reimport(str(p))
    assert out['ok']
    assert out['elements'] == ['Fe', 'O']       # FE→Fe(大小写),8→O(原子序)


def test_reimport_bad_xyz_count(tmp_path):
    p = tmp_path / 'bad.xyz'
    p.write_text('5\ncmt\nO 0 0 0\n', encoding='utf-8')   # 声明 5 实得 1
    out = ee.reimport(str(p))
    assert not out['ok'] and '原子数不符' in out['error']


def test_reimport_mol_by_content_sniff(tmp_path):
    # 无扩展名但含 V2000/M END → 内容嗅探判为 MOL
    r = ee.export_for_editor(_ELS, _XYZ, 'mol', workdir=str(tmp_path))
    noext = tmp_path / 'struct'
    noext.write_text(open(r['path'], encoding='utf-8').read(), encoding='utf-8')
    out = ee.reimport(str(noext))
    assert out['ok'] and out['elements'] == _ELS


def test_open_with_editor_exe(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))
    calls = []
    out = ee.open_with(r['path'], 'gaussview.exe', popen=lambda args: calls.append(args))
    assert out['ok'] and calls == [['gaussview.exe', r['path']]]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX desktop opener only')
def test_open_with_default_posix(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))
    calls = []
    out = ee.open_with(r['path'], popen=lambda args: calls.append(args))
    assert out['ok'] and calls[-1] == ['xdg-open', r['path']]


def test_open_with_missing_file(tmp_path):
    out = ee.open_with(str(tmp_path / 'nope.xyz'), popen=lambda args: None)
    assert not out['ok'] and '不存在' in out['error']


def test_open_with_popen_exception_to_chinese(tmp_path):
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))

    def _boom(args):
        raise OSError('no such editor')

    out = ee.open_with(r['path'], 'ghost.exe', popen=_boom)
    assert not out['ok'] and '打开外部编辑器失败' in out['error']


def test_open_with_windows_startfile_branch(tmp_path, monkeypatch):
    r = ee.export_for_editor(_ELS, _XYZ, 'xyz', workdir=str(tmp_path))
    monkeypatch.setattr(ee.os, 'name', 'nt')
    opened = []
    out = ee.open_with(r['path'], startfile=lambda p: opened.append(p))
    assert out['ok'] and opened == [r['path']]
