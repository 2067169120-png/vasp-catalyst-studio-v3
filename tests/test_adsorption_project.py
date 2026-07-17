"""吸附能项目测试:批量生成 / project.yaml / ΔE 门控 / CSV 导出。"""
import os

import pytest

from vcstudio.cluster import ledger
from vcstudio.project import adsorption
from vcstudio.shared import manifest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """假赝势库(C) + 三个 POSCAR + 注册表/台账全部指向 tmp(不碰真实 %APPDATA%)。"""
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   ENMAX  =  273.214; ENMIN = 200.000 eV\n', encoding='utf-8')

    def poscar(name, element='C'):
        p = tmp_path / name
        p.write_text(f'{name}\n1.0\n10 0 0\n0 10 0\n0 0 10\n{element}\n1\nCartesian\n0 0 0\n',
                     encoding='utf-8')
        return str(p)

    incar = tmp_path / 'INCAR'
    incar.write_text('ENCUT = 400\nISMEAR = 0\n', encoding='utf-8')
    monkeypatch.setattr(ledger, 'default_ledger_path', lambda: tmp_path / 'jobs.json')
    monkeypatch.setattr(adsorption, 'default_registry_path', lambda: tmp_path / 'projects.json')
    return {'tmp': tmp_path, 'lib': str(lib), 'incar': str(incar), 'poscar': poscar}


def _finish(job_dir, energy):
    """把成员标成 DONE 并回填能量(模拟跑完集群)。"""
    m = manifest.load_manifest(job_dir)
    manifest.set_state(m, 'DONE')
    m.setdefault('results', {})['energy_e0_eV'] = energy
    manifest.save_manifest(job_dir, m)


def test_create_project_generates_members_and_registers(env):
    res = adsorption.create_project(
        env['tmp'] / 'proj', 'liS', clean_poscar=env['poscar']('slab.vasp'),
        config_poscars=[env['poscar']('h1.vasp'), env['poscar']('b2.vasp')],
        incar_path=env['incar'], ref_poscar=env['poscar']('mol.vasp'),
        lib_root=env['lib'])
    assert res['ok'] and not res['errors']
    proj = adsorption.load_project(res['project_path'])
    assert proj['name'] == 'liS'
    assert len(proj['members']['configs']) == 2 and proj['members']['gas_ref']
    # 参考分子按 molecule → KPOINTS Γ 点
    ref_m = manifest.load_manifest(proj['members']['gas_ref'])
    assert ref_m['calc_type'] == 'molecule' and ref_m['inputs']['kpoints'] == [1, 1, 1]
    # 全部登记进台账 + 项目注册表
    assert len(ledger.list_dirs()) == 4
    assert adsorption.list_projects() == [res['project_path']]


def test_bad_config_isolated_others_survive(env):
    res = adsorption.create_project(
        env['tmp'] / 'p2', 'x', clean_poscar=env['poscar']('s.vasp'),
        config_poscars=[env['poscar']('bad.vasp', element='Xx'), env['poscar']('ok.vasp')],
        incar_path=env['incar'], lib_root=env['lib'])
    assert len(res['errors']) == 1 and 'Xx' in res['errors'][0][1]
    proj = adsorption.load_project(res['project_path'])
    assert len(proj['members']['configs']) == 1        # 坏构型不入组


def test_project_unifies_encut_across_members(tmp_path, monkeypatch):
    """修复:INCAR 未给 ENCUT 时,项目内各成员按**元素并集**统一补同一 ENCUT,
    防 ΔE=E(slab+ads)−E(slab)−E(ref) 被不同截断能静默污染(缺口分析主打功能错误)。"""
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'O').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' PAW_PBE C\n   ENMAX  =  273.214 eV\n', encoding='utf-8')
    (lib / 'O' / 'POTCAR').write_text(
        ' PAW_PBE O\n   ENMAX  =  400.000 eV\n', encoding='utf-8')
    monkeypatch.setattr(ledger, 'default_ledger_path', lambda: tmp_path / 'jobs.json')
    monkeypatch.setattr(adsorption, 'default_registry_path', lambda: tmp_path / 'projects.json')

    incar = tmp_path / 'INCAR'          # 关键:不给 ENCUT
    incar.write_text('ISMEAR = 0\n', encoding='utf-8')

    def poscar(name, species, counts):
        p = tmp_path / name
        p.write_text(f'{name}\n1.0\n10 0 0\n0 10 0\n0 0 10\n{species}\n{counts}\n'
                     'Cartesian\n0 0 0\n', encoding='utf-8')
        return str(p)

    res = adsorption.create_project(
        tmp_path / 'proj', 'liS',
        clean_poscar=poscar('slab.vasp', 'C', '1'),          # 仅 C
        config_poscars=[poscar('c1.vasp', 'C O', '1 1')],    # C+O
        incar_path=str(incar),
        ref_poscar=poscar('ref.vasp', 'O', '1'),             # 仅 O
        lib_root=str(lib))
    assert res['ok'] and not res['errors']
    proj = adsorption.load_project(res['project_path'])

    # 并集 {C,O} → max ENMAX=400 → 统一 ENCUT=ceil(1.3*400/50)*50=550;全员一致
    dirs = [proj['members']['clean_slab'], proj['members']['gas_ref'],
            *proj['members']['configs']]
    encuts = {manifest.load_manifest(d)['inputs']['completions']['ENCUT'] for d in dirs}
    assert encuts == {550}, f'各成员 ENCUT 未统一: {encuts}'


def test_delta_e_gating_and_value(env):
    res = adsorption.create_project(
        env['tmp'] / 'p3', 'd', clean_poscar=env['poscar']('s.vasp'),
        config_poscars=[env['poscar']('c1.vasp')], incar_path=env['incar'],
        ref_poscar=env['poscar']('r.vasp'), lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])

    s = adsorption.delta_e_rows(proj)                  # 全员未完成 → 不给数
    assert s['rows'][0]['delta_e'] is None
    assert '未完成' in s['rows'][0]['note']

    _finish(proj['members']['clean_slab'], -400.0)
    _finish(proj['members']['configs'][0], -435.5)
    s = adsorption.delta_e_rows(proj)                  # 参考还没完 → 仍不给数
    assert s['rows'][0]['delta_e'] is None and '参考' in s['rows'][0]['note']

    _finish(proj['members']['gas_ref'], -21.0)
    s = adsorption.delta_e_rows(proj)
    assert s['rows'][0]['delta_e'] == pytest.approx(-435.5 - (-400.0) - (-21.0))


def test_delta_e_without_ref_notes_formula(env):
    res = adsorption.create_project(
        env['tmp'] / 'p4', 'nr', clean_poscar=env['poscar']('s.vasp'),
        config_poscars=[env['poscar']('c1.vasp')], incar_path=env['incar'],
        lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    _finish(proj['members']['clean_slab'], -400.0)
    _finish(proj['members']['configs'][0], -435.5)
    s = adsorption.delta_e_rows(proj)
    assert s['rows'][0]['delta_e'] == pytest.approx(-35.5)
    assert 'E(slab+ads)−E(slab)' in s['rows'][0]['note']


def test_export_csv_excel_friendly(env, tmp_path):
    res = adsorption.create_project(
        env['tmp'] / 'p5', 'csv', clean_poscar=env['poscar']('s.vasp'),
        config_poscars=[env['poscar']('c1.vasp')], incar_path=env['incar'],
        ref_poscar=env['poscar']('r.vasp'), lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    for d, e in ((proj['members']['clean_slab'], -400.0),
                 (proj['members']['configs'][0], -435.5),
                 (proj['members']['gas_ref'], -21.0)):
        _finish(d, e)
    out = adsorption.export_csv(proj, adsorption.delta_e_rows(proj),
                                tmp_path / 'out' / 'd.csv')
    raw = out.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')             # utf-8-sig BOM(Excel 中文不乱码)
    text = raw.decode('utf-8-sig')
    assert '构型' in text and 'ΔE_ads / eV' in text
    assert '-14.500000' in text                        # -435.5 + 400 + 21


def test_delta_e_most_stable_grouping(env):
    """多构型取最稳:同 species 组内 ΔE 最低者 is_most_stable,dd_e 为相对最稳的 ΔΔE。"""
    res = adsorption.create_project(
        env['tmp'] / 'pms', 'ms', clean_poscar=env['poscar']('slab.vasp'),
        config_poscars=[env['poscar']('Li2S4_top.vasp'),
                        env['poscar']('Li2S4_hollow.vasp'),
                        env['poscar']('Li2S6_top.vasp')],
        incar_path=env['incar'], ref_poscar=env['poscar']('ref.vasp'),
        lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    _finish(proj['members']['clean_slab'], -400.0)
    _finish(proj['members']['gas_ref'], -20.0)
    energies = {'ms_ads_Li2S4_top': -450.0, 'ms_ads_Li2S4_hollow': -448.0,
                'ms_ads_Li2S6_top': -455.0}
    for d in proj['members']['configs']:
        _finish(d, energies[os.path.basename(d)])

    s = adsorption.delta_e_rows(proj)
    by = {r['name']: r for r in s['rows']}
    # species 从成员名剥 '{项目名}_ads_' 前缀识别为化学式 token
    assert by['ms_ads_Li2S4_top']['species'] == 'Li2S4'
    assert by['ms_ads_Li2S4_hollow']['species'] == 'Li2S4'
    assert by['ms_ads_Li2S6_top']['species'] == 'Li2S6'
    # Li2S4 组:top(ΔE=-30)最稳;hollow(ΔE=-28)落后 2.0 eV
    assert by['ms_ads_Li2S4_top']['is_most_stable'] is True
    assert by['ms_ads_Li2S4_top']['dd_e'] == pytest.approx(0.0)
    assert by['ms_ads_Li2S4_hollow']['is_most_stable'] is False
    assert by['ms_ads_Li2S4_hollow']['dd_e'] == pytest.approx(2.0)
    # Li2S6 单构型 → 自成最稳
    assert by['ms_ads_Li2S6_top']['is_most_stable'] is True
    assert by['ms_ads_Li2S6_top']['dd_e'] == 0.0

    # CSV 新增两列且标出最稳位
    out = adsorption.export_csv(proj, s, env['tmp'] / 'ms.csv')
    text = out.read_bytes().decode('utf-8-sig')
    assert 'ΔΔE(eV)' in text and '是否最稳' in text
    assert '2.000000' in text and '是' in text


def test_delta_e_species_falls_back_to_short_name(env):
    """识别不出化学式 token(如 h1/b2)→ species 用短名,各自成组。"""
    res = adsorption.create_project(
        env['tmp'] / 'pfb', 'fb', clean_poscar=env['poscar']('s.vasp'),
        config_poscars=[env['poscar']('h1.vasp'), env['poscar']('b2.vasp')],
        incar_path=env['incar'], lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    _finish(proj['members']['clean_slab'], -400.0)
    for d in proj['members']['configs']:
        _finish(d, -420.0)
    by = {r['name']: r for r in adsorption.delta_e_rows(proj)['rows']}
    assert by['fb_ads_h1']['species'] == 'h1'
    assert by['fb_ads_b2']['species'] == 'b2'
    # 各自单独一组 → 都是各自组内最稳
    assert by['fb_ads_h1']['is_most_stable'] and by['fb_ads_b2']['is_most_stable']
