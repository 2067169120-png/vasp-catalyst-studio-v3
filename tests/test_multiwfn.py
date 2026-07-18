"""Multiwfn 驱动测试:探测、每个 stdin 脚本生成(含参数注入)、run 编排(假 subprocess)、
降级回传脚本、ESP 极值解析(逼真 fixture)。全程 mock subprocess,不真跑 Multiwfn。"""
import os

import pytest

from vcstudio.external import multiwfn_driver as md

# ── 逼真 fixture:Multiwfn 主功能12 定量分子表面分析的 ESP 表面极值输出 ─────────────────
_ESP_EXTREMA = """
 ================= Summary of surface analysis =================

 Volume:   543.21 Bohr^3  (  80.501 Angstrom^3)
 Global surface minimum value:  -45.231 kcal/mol
 Global surface maximum value:   38.912 kcal/mol
 Overall surface area:  312.4 Bohr^2

 Surface local minima (kcal/mol):
   Index         X            Y            Z            Value
      1     -2.345678     1.234567     0.100000       -45.231
      2      1.500000    -0.900000     2.300000       -30.114
      3      0.000000     0.000000    -3.400000       -12.507

 Surface local maxima (kcal/mol):
   Index         X            Y            Z            Value
      1     -1.111100     2.222200     3.333300        38.912
      2      2.500000    -2.500000     0.000000        21.006
"""


# ── 探测 ─────────────────────────────────────────────────────────────────────
def test_probe_explicit_path(tmp_path):
    exe = tmp_path / 'Multiwfn'
    exe.write_text('#!/bin/sh\n', encoding='utf-8')
    info = md.probe(str(exe))
    assert info['available'] and info['path'] == str(exe)


def test_probe_missing(monkeypatch):
    monkeypatch.setattr(md.shutil, 'which', lambda name: None)
    info = md.probe()
    assert not info['available'] and info['path'] is None and 'Multiwfn' in info['detail']


def test_probe_found_on_path(monkeypatch):
    monkeypatch.setattr(md.shutil, 'which',
                        lambda name: '/usr/bin/Multiwfn' if name == 'Multiwfn' else None)
    info = md.probe()
    assert info['available'] and info['path'] == '/usr/bin/Multiwfn'


def test_probe_explicit_missing(monkeypatch):
    monkeypatch.setattr(md.shutil, 'which', lambda name: None)
    info = md.probe('/no/such/Multiwfn')
    assert not info['available'] and '不存在' in info['detail']


# ── 分析项注册表 ─────────────────────────────────────────────────────────────
def test_analyses_registry_shape():
    assert set(md.ANALYSES) == {'esp_extrema', 'homo_lumo_cube', 'density_cube',
                                'esp_cube', 'nci_rdg', 'igmh', 'aim_cp',
                                'alie', 'alie_extrema', 'iri'}
    for spec in md.ANALYSES.values():
        assert callable(spec['stdin_script'])
        assert isinstance(spec['name'], str) and spec['name']
        assert isinstance(spec['outputs'], tuple)
        assert isinstance(spec['note'], str) and spec['note']


# ── 每个 stdin 脚本生成(含参数注入) ────────────────────────────────────────────
def test_script_esp_extrema():
    assert md.ANALYSES['esp_extrema']['stdin_script']({}).splitlines()[:2] == ['12', '0']


def test_script_density_and_esp_cube():
    assert md.ANALYSES['density_cube']['stdin_script']({}).splitlines()[:2] == ['5', '1']
    assert md.ANALYSES['esp_cube']['stdin_script']({}).splitlines()[:2] == ['5', '12']


def test_script_orbital_injects_index():
    lines = md.ANALYSES['homo_lumo_cube']['stdin_script']({'orbital': 45}).splitlines()
    assert lines[:2] == ['5', '4'] and '45' in lines
    assert 'HOMO' in md.ANALYSES['homo_lumo_cube']['stdin_script']({'orbital': 'homo'}).splitlines()
    assert 'HOMO' in md.ANALYSES['homo_lumo_cube']['stdin_script']({}).splitlines()  # 默认 HOMO


def test_script_nci_rdg():
    assert md.ANALYSES['nci_rdg']['stdin_script']({}).splitlines()[:2] == ['20', '1']


def test_script_igmh_injects_fragments_and_requires_them():
    lines = md.ANALYSES['igmh']['stdin_script']({'fragments': ['1-12', '13-20']}).splitlines()
    assert lines[:2] == ['20', '11'] and lines[2] == '2'   # 片段数量=2
    assert '1-12' in lines and '13-20' in lines
    with pytest.raises(ValueError, match='fragments'):
        md.ANALYSES['igmh']['stdin_script']({})


def test_script_aim_cp():
    lines = md.ANALYSES['aim_cp']['stdin_script']({}).splitlines()
    assert lines[0] == '2' and '7' in lines


# ── ALIE / IRI 补全种类(对齐 starpivot 七件套外的两种) ─────────────────────────────
def test_script_alie_cube():
    # 主功能5 → 实空间函数 ALIE(常量)→ 导出 cube
    lines = md.ANALYSES['alie']['stdin_script']({}).splitlines()
    assert lines[0] == '5' and lines[1] == md._FUNC_ALIE
    assert lines[3] == '2'                                       # 导出 cube
    assert md.ANALYSES['alie']['outputs'] == ('ALIE.cub',)


def test_script_alie_cube_grid_override():
    assert md.ANALYSES['alie']['stdin_script']({'grid': 3}).splitlines()[2] == '3'


def test_script_alie_extrema():
    # 主功能12 → 选映射函数(常量)→ ALIE(常量)→ 0 开始
    lines = md.ANALYSES['alie_extrema']['stdin_script']({}).splitlines()
    assert lines[0] == '12' and lines[1] == md._SURF_SELECT_MAPPED
    assert lines[2] == md._SURF_MAPPED_ALIE and lines[3] == '0'
    assert md.ANALYSES['alie_extrema']['outputs'] == ()          # 极值走 stdout,无文件产物


def test_script_iri():
    # 主功能20 → IRI 子功能(常量);产物 func1.cub(IRI)+func2.cub(sign(λ2)ρ)
    lines = md.ANALYSES['iri']['stdin_script']({}).splitlines()
    assert lines[0] == '20' and lines[1] == md._SUB_IRI
    assert md.ANALYSES['iri']['outputs'] == ('func1.cub', 'func2.cub')


def test_script_iri_grid_override():
    assert md.ANALYSES['iri']['stdin_script']({'grid': 3}).splitlines()[2] == '3'


def test_script_grid_param_override():
    # 格点质量参数注入:params['grid'] 落到密度 cube 脚本第 3 行
    assert md.ANALYSES['density_cube']['stdin_script']({'grid': 3}).splitlines()[2] == '3'


# ── run 编排(假 subprocess) ──────────────────────────────────────────────────
def test_run_assembles_cmd_and_collects_renamed_cubes(tmp_path, monkeypatch):
    wf = tmp_path / 'mol.fchk'
    wf.write_text('fake wavefn', encoding='utf-8')
    monkeypatch.setattr(md, 'probe',
                        lambda exe=None: {'available': True, 'path': 'Multiwfn', 'detail': ''})
    captured = {}

    def fake_run(cmd, **kw):
        captured['cmd'] = cmd
        captured['input'] = kw.get('input')
        captured['cwd'] = kw.get('cwd')
        for name in ('func1.cub', 'func2.cub'):        # 模拟 NCI 产出两 cube
            with open(os.path.join(kw['cwd'], name), 'wb') as f:
                f.write(b'CUBE')

        class P:
            returncode = 0
            stdout = b'NCI analysis done\n'
        return P()

    monkeypatch.setattr(md.subprocess, 'run', fake_run)
    out = md.run(str(wf), 'nci_rdg', workdir=str(tmp_path))
    assert out['ok']
    names = sorted(os.path.basename(p) for p in out['outputs'])
    assert names == ['nci_rdg_func1.cub', 'nci_rdg_func2.cub']       # 带 analysis 前缀重命名
    assert captured['cmd'][0] == 'Multiwfn' and captured['cmd'][1].endswith('mol.fchk')
    assert captured['input'].decode('utf-8').startswith('20')        # NCI 从主功能20 起
    assert out['elapsed_s'] >= 0.0


def test_run_iri_collects_prefixed_cubes(tmp_path, monkeypatch):
    wf = tmp_path / 'mol.fchk'
    wf.write_text('fake wavefn', encoding='utf-8')
    monkeypatch.setattr(md, 'probe',
                        lambda exe=None: {'available': True, 'path': 'Multiwfn', 'detail': ''})

    def fake_run(cmd, **kw):
        for name in ('func1.cub', 'func2.cub'):        # 模拟 IRI 产出两 cube
            with open(os.path.join(kw['cwd'], name), 'wb') as f:
                f.write(b'CUBE')

        class P:
            returncode = 0
            stdout = b'IRI analysis done\n'
        return P()

    monkeypatch.setattr(md.subprocess, 'run', fake_run)
    out = md.run(str(wf), 'iri', workdir=str(tmp_path))
    assert out['ok']
    names = sorted(os.path.basename(p) for p in out['outputs'])
    assert names == ['iri_func1.cub', 'iri_func2.cub']          # 带 iri_ 前缀重命名


def test_run_alie_extrema_ok_via_stdout(tmp_path, monkeypatch):
    wf = tmp_path / 'mol.wfx'
    wf.write_text('x', encoding='utf-8')
    monkeypatch.setattr(md, 'probe',
                        lambda exe=None: {'available': True, 'path': 'Multiwfn', 'detail': ''})

    def fake_run(cmd, **kw):
        assert kw['input'].decode('utf-8').startswith('12')     # 定量分子表面分析
        class P:
            returncode = 0
            stdout = b'Surface local minima (eV)\n 12.3 eV\n'
        return P()

    monkeypatch.setattr(md.subprocess, 'run', fake_run)
    out = md.run(str(wf), 'alie_extrema', workdir=str(tmp_path))
    assert out['ok'] and out['outputs'] == []


def test_run_esp_extrema_ok_via_stdout(tmp_path, monkeypatch):
    wf = tmp_path / 'mol.wfx'
    wf.write_text('x', encoding='utf-8')
    monkeypatch.setattr(md, 'probe',
                        lambda exe=None: {'available': True, 'path': 'Multiwfn', 'detail': ''})

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = b'Surface local minima\n -45.2 kcal/mol\n'
        return P()

    monkeypatch.setattr(md.subprocess, 'run', fake_run)
    out = md.run(str(wf), 'esp_extrema', workdir=str(tmp_path))
    assert out['ok'] and out['outputs'] == [] and 'minima' in out['stdout_tail']


def test_run_reports_missing_products(tmp_path, monkeypatch):
    wf = tmp_path / 'mol.molden'
    wf.write_text('x', encoding='utf-8')
    monkeypatch.setattr(md, 'probe',
                        lambda exe=None: {'available': True, 'path': 'Multiwfn', 'detail': ''})

    def fake_run(cmd, **kw):
        class P:                                    # 什么都不产出
            returncode = 0
            stdout = b''
        return P()

    monkeypatch.setattr(md.subprocess, 'run', fake_run)
    out = md.run(str(wf), 'density_cube', workdir=str(tmp_path))
    assert not out['ok'] and '未找到预期产物' in out['error']


def test_run_degrades_without_multiwfn_returns_script(tmp_path, monkeypatch):
    wf = tmp_path / 'mol.wfn'
    wf.write_text('x', encoding='utf-8')
    monkeypatch.setattr(md, 'probe',
                        lambda exe=None: {'available': False, 'path': None, 'detail': '未找到 Multiwfn'})
    out = md.run(str(wf), 'density_cube', workdir=str(tmp_path))
    assert not out['ok'] and 'script' in out
    assert out['script'].splitlines()[:2] == ['5', '1']              # 回传可手动运行的脚本


def test_run_rejects_bad_wavefn_ext(tmp_path):
    out = md.run(str(tmp_path / 'mol.txt'), 'density_cube')
    assert not out['ok'] and '不支持' in out['error'] and 'script' in out


def test_run_unknown_analysis():
    out = md.run('mol.wfn', 'no_such_analysis')
    assert not out['ok'] and 'no_such_analysis' in out['error']


def test_run_timeout_degrades(tmp_path, monkeypatch):
    wf = tmp_path / 'mol.wfn'
    wf.write_text('x', encoding='utf-8')
    monkeypatch.setattr(md, 'probe',
                        lambda exe=None: {'available': True, 'path': 'Multiwfn', 'detail': ''})

    def fake_run(cmd, **kw):
        raise md.subprocess.TimeoutExpired(cmd, kw.get('timeout', 1))

    monkeypatch.setattr(md.subprocess, 'run', fake_run)
    out = md.run(str(wf), 'aim_cp', workdir=str(tmp_path))
    assert not out['ok'] and '运行失败' in out['error']


# ── ESP 极值解析 ─────────────────────────────────────────────────────────────
def test_extrema_parse():
    res = md.extrema_parse(_ESP_EXTREMA)
    assert len(res['minima']) == 3 and len(res['maxima']) == 2
    assert res['minima'][0]['value_kcal'] == pytest.approx(-45.231)
    assert res['minima'][0]['xyz'] == pytest.approx([-2.345678, 1.234567, 0.1])
    assert res['maxima'][0]['value_kcal'] == pytest.approx(38.912)
    assert res['maxima'][0]['xyz'] == pytest.approx([-1.1111, 2.2222, 3.3333])


def test_extrema_parse_empty():
    res = md.extrema_parse('no extrema here\n')
    assert res == {'minima': [], 'maxima': []}
