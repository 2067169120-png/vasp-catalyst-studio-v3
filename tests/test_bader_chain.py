"""Bader 链测试(project.bader 新增):exe 探测、AECCAR 合并、run_bader 降级、ΔQ 字典/分组。"""
import os

import pytest

from vcstudio.project import bader, chgdiff

_CUBE = [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 3.0]]

# 2 原子 ACF(与 test_bader 同源)
_ACF = """\
    #         X           Y           Z       CHARGE      MIN DIST   ATOMIC VOL
 --------------------------------------------------------------------------------
    1    0.000000    0.000000    0.000000    8.147852     0.371289     7.867616
    2    1.425000    1.425000    1.425000    1.852148     0.371289     8.126547
 --------------------------------------------------------------------------------
    NUMBER OF ELECTRONS:        10.0000
"""


def _chgcar(grid, ng=(2, 2, 2)):
    lines = ['synthetic', '1.0']
    for v in _CUBE:
        lines.append(f' {v[0]} {v[1]} {v[2]}')
    lines += [' Cu', ' 1', 'Direct', ' 0.0 0.0 0.0', '', f' {ng[0]} {ng[1]} {ng[2]}']
    for i in range(0, len(grid), 5):
        lines.append(' '.join(str(v) for v in grid[i:i + 5]))
    return '\n'.join(lines) + '\n'


# ── find_bader_exe ───────────────────────────────────────────────────────────

def test_find_bader_exe_none(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda name: None)
    assert bader.find_bader_exe() is None


def test_find_bader_exe_configured(tmp_path):
    fake = tmp_path / 'bader'
    fake.write_text('#!/bin/sh\n', encoding='utf-8')
    assert bader.find_bader_exe(str(fake)) == str(fake)


# ── sum_aeccar(纯 python 网格代数)──────────────────────────────────────────

def test_sum_aeccar_hand_computed(tmp_path):
    a0 = tmp_path / 'AECCAR0'
    a2 = tmp_path / 'AECCAR2'
    a0.write_text(_chgcar([float(i) for i in range(1, 9)]), encoding='utf-8')
    a2.write_text(_chgcar([10.0 * i for i in range(1, 9)]), encoding='utf-8')
    out = tmp_path / 'CHGCAR_sum'
    res = bader.sum_aeccar(str(a0), str(a2), str(out))
    assert res['n_grid'] == 8
    summed = chgdiff.read_chgcar(str(out))['grid']
    assert summed == pytest.approx([11, 22, 33, 44, 55, 66, 77, 88])


def test_sum_aeccar_grid_mismatch(tmp_path):
    a0 = tmp_path / 'AECCAR0'
    a2 = tmp_path / 'AECCAR2'
    a0.write_text(_chgcar([1.0] * 8, ng=(2, 2, 2)), encoding='utf-8')
    a2.write_text(_chgcar([1.0] * 4, ng=(2, 2, 1)), encoding='utf-8')
    with pytest.raises(ValueError, match='网格不一致'):
        bader.sum_aeccar(str(a0), str(a2), tmp_path / 'sum')


# ── run_bader:探测→调用→降级 ───────────────────────────────────────────────

def _write_bader_inputs(job):
    job.mkdir()
    (job / 'AECCAR0').write_text(_chgcar([float(i) for i in range(1, 9)]), encoding='utf-8')
    (job / 'AECCAR2').write_text(_chgcar([1.0] * 8), encoding='utf-8')
    (job / 'CHGCAR').write_text(_chgcar([2.0] * 8), encoding='utf-8')


def test_run_bader_missing_inputs(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    res = bader.run_bader(str(job))
    assert res['ok'] is False
    assert 'LAECHG' in res['error']


def test_run_bader_exe_missing_prepares_sum(tmp_path, monkeypatch):
    job = tmp_path / 'job'
    _write_bader_inputs(job)
    monkeypatch.setattr('shutil.which', lambda name: None)     # bader 不在 PATH
    res = bader.run_bader(str(job))
    assert res['ok'] is False
    assert '未找到 bader' in res['error']
    assert os.path.isfile(res['chgcar_sum'])                   # 已备好参考
    summed = chgdiff.read_chgcar(res['chgcar_sum'])['grid']
    assert summed == pytest.approx([2, 3, 4, 5, 6, 7, 8, 9])   # (1..8)+1


def test_run_bader_success_injected_run(tmp_path):
    job = tmp_path / 'job'
    _write_bader_inputs(job)

    class _Proc:
        returncode = 0

    def fake_run(cmd, cwd=None, timeout=None, **kw):
        assert cmd[0].endswith('bader') and '-ref' in cmd
        with open(os.path.join(cwd, 'ACF.dat'), 'w', encoding='utf-8') as f:
            f.write(_ACF)
        return _Proc()

    res = bader.run_bader(str(job), exe='/usr/bin/bader', run=fake_run)
    assert res['ok'] is True
    assert res['n_atoms'] == 2
    assert res['charges'] == pytest.approx([8.147852, 1.852148])
    assert os.path.isfile(res['acf_path'])


def test_run_bader_nonzero_exit(tmp_path):
    job = tmp_path / 'job'
    _write_bader_inputs(job)

    class _Proc:
        returncode = 1

    res = bader.run_bader(str(job), exe='/usr/bin/bader',
                          run=lambda *a, **k: _Proc())
    assert res['ok'] is False
    assert '退出码 1' in res['error']


# ── parse_acf 字典形态 + 守恒 + group_transfer ───────────────────────────────

def test_parse_acf_dict_form_and_conservation():
    # 单参仍返回列表(向后兼容)
    assert bader.parse_acf(_ACF) == pytest.approx([8.147852, 1.852148])
    # 带 ZVAL → 字典;Fe(ZVAL8)得电子,X(ZVAL2)失电子,∑ΔQ≈0 无 warning
    d = bader.parse_acf(_ACF, [8.0, 2.0])
    assert d['delta_q'] == pytest.approx([-0.147852, 0.147852])
    assert d['sum_delta_q'] == pytest.approx(0.0)
    assert d['atoms'][0] == {'index': 1, 'charge': pytest.approx(8.147852),
                             'delta_q': pytest.approx(-0.147852)}
    assert d['warnings'] == []


def test_parse_acf_conservation_warning():
    d = bader.parse_acf(_ACF, [9.0, 2.0])          # ∑ΔQ = 0.852 ≫ 0.05
    assert abs(d['sum_delta_q']) > 0.05
    assert any('不守恒' in w for w in d['warnings'])


def test_parse_acf_zval_length_mismatch():
    with pytest.raises(ValueError, match='原子数不匹配'):
        bader.parse_acf(_ACF, [8.0])


def test_group_transfer_from_dict_and_list():
    d = bader.parse_acf(_ACF, [8.0, 2.0])          # ΔQ = [-0.147852, +0.147852]
    g = bader.group_transfer(d, [1])               # 吸附质 = 原子 1(ΔQ<0,得电子)
    assert g['net_delta_q'] == pytest.approx(-0.147852)
    assert g['electrons_gained'] == pytest.approx(0.147852)
    assert g['n_atoms'] == 1
    # 也接受逐原子 ΔQ 列表
    g2 = bader.group_transfer([-0.5, 0.2, 0.3], [2, 3])
    assert g2['net_delta_q'] == pytest.approx(0.5)
    assert g2['electrons_gained'] == pytest.approx(-0.5)


def test_group_transfer_errors():
    d = bader.parse_acf(_ACF, [8.0, 2.0])
    with pytest.raises(ValueError, match='越界'):
        bader.group_transfer(d, [99])
    with pytest.raises(ValueError, match='为空'):
        bader.group_transfer(d, [])
    with pytest.raises(ValueError, match='delta_q'):
        bader.group_transfer({'charges': [1.0]}, [1])
