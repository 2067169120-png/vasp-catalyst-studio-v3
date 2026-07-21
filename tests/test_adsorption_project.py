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
    with open(os.path.join(job_dir, 'OSZICAR'), 'w', encoding='utf-8') as handle:
        handle.write(f' 1 F= {energy:.12f} E0= {energy:.12f} d E =0\n')
    with open(os.path.join(job_dir, 'OUTCAR'), 'w', encoding='utf-8') as handle:
        handle.write('General timing and accounting information for this job\n')


def _make_species_ref_job(job_dir, *, energy=-38.072771, state='DONE',
                          source='OSZICAR:E0'):
    """Create a molecular reference manifest for species-reference tests."""
    job_dir.mkdir(parents=True, exist_ok=True)
    ref = manifest.new_manifest(job_id='ref', system='Li2S8', task_type='relax',
                                calc_type='molecule', inputs={})
    manifest.set_state(ref, state)
    ref['results'].update(energy_e0_eV=energy, energy_source=source)
    manifest.save_manifest(job_dir, ref)
    (job_dir / 'OSZICAR').write_text(
        f' 1 F= {energy:.12f} E0= {energy:.12f} d E =0\n', encoding='utf-8')
    (job_dir / 'OUTCAR').write_text(
        'General timing and accounting information for this job\n', encoding='utf-8')
    return job_dir


def _set_actual_method(job_dir, functional, *, ispin=1):
    item = manifest.load_manifest(job_dir)
    item.setdefault('results', {})['reference_method_signature'] = {
        'functional': functional, 'ivdw': 0, 'encut': 400.0, 'ispin': ispin,
        'ldau': 'F', 'potcar_titel': ['PAW_PBE C'], 'potcar_elements': ['C'],
    }
    manifest.save_manifest(job_dir, item)


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
    assert adsorption.unregister_project(res['project_path']) is True
    assert adsorption.list_projects() == []
    assert adsorption.unregister_project(res['project_path']) is False
    assert adsorption.register_project(res['project_path']) is True


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


def test_delta_e_blocks_done_operand_when_oszicar_is_missing(env):
    """DONE/job.yaml 的缓存能量不能代替当前轮 OSZICAR 证据。"""
    result = adsorption.create_project(
        env['tmp'] / 'missing-oszicar', 'missing-oszicar',
        clean_poscar=env['poscar']('missing-oszicar-slab.vasp'),
        config_poscars=[env['poscar']('missing-oszicar-config.vasp')],
        incar_path=env['incar'], ref_poscar=env['poscar']('missing-oszicar-ref.vasp'),
        lib_root=env['lib'])
    project = adsorption.load_project(result['project_path'])
    clean = project['members']['clean_slab']
    config = project['members']['configs'][0]
    reference = project['members']['gas_ref']
    _finish(clean, -100.0)
    _finish(config, -115.0)
    _finish(reference, -10.0)
    os.unlink(os.path.join(clean, 'OSZICAR'))

    row = adsorption.delta_e_rows(project)['rows'][0]

    assert row['delta_e'] is None
    assert '清洁表面缺少可解析的当前轮 OSZICAR:E0' in row['note']


def test_delta_e_blocks_done_operand_when_oszicar_energy_was_replaced(env):
    """后续被覆盖的 OSZICAR 不得与旧 job.yaml 能量混用。"""
    result = adsorption.create_project(
        env['tmp'] / 'mutated-oszicar', 'mutated-oszicar',
        clean_poscar=env['poscar']('mutated-oszicar-slab.vasp'),
        config_poscars=[env['poscar']('mutated-oszicar-config.vasp')],
        incar_path=env['incar'], ref_poscar=env['poscar']('mutated-oszicar-ref.vasp'),
        lib_root=env['lib'])
    project = adsorption.load_project(result['project_path'])
    clean = project['members']['clean_slab']
    config = project['members']['configs'][0]
    reference = project['members']['gas_ref']
    _finish(clean, -100.0)
    _finish(config, -115.0)
    _finish(reference, -10.0)
    with open(os.path.join(config, 'OSZICAR'), 'w', encoding='utf-8') as handle:
        handle.write(' 1 F= -999.0 E0= -999.0 d E =0\n')

    row = adsorption.delta_e_rows(project)['rows'][0]

    assert row['delta_e'] is None
    assert 'job.yaml 能量 -115.00000000 eV 与 OSZICAR -999.00000000 eV 不一致' in row['note']


def test_delta_e_blocks_known_pbe_rpbe_method_mismatch(env):
    result = adsorption.create_project(
        env['tmp'] / 'method-mismatch', 'mix',
        clean_poscar=env['poscar']('method-slab.vasp'),
        config_poscars=[env['poscar']('method-config.vasp')],
        incar_path=env['incar'], lib_root=env['lib'])
    project = adsorption.load_project(result['project_path'])
    clean = project['members']['clean_slab']
    config = project['members']['configs'][0]
    _finish(clean, -100.0)
    _finish(config, -110.0)
    _set_actual_method(clean, 'PBE')
    _set_actual_method(config, 'RPBE')

    summary = adsorption.delta_e_rows(project)
    row = summary['rows'][0]

    assert row['delta_e'] is None
    assert row['method_check']['status'] == 'incompatible'
    assert '方法不一致' in row['note'] and '泛函' in row['note']
    assert summary['method_consistency']['status'] == 'incompatible'


def test_delta_e_allows_molecular_ispin_difference_with_audit_warning(env):
    result = adsorption.create_project(
        env['tmp'] / 'spin-reference', 'spin-reference',
        clean_poscar=env['poscar']('spin-slab.vasp'),
        config_poscars=[env['poscar']('spin-config.vasp')],
        incar_path=env['incar'], ref_poscar=env['poscar']('spin-molecule.vasp'),
        lib_root=env['lib'])
    project = adsorption.load_project(result['project_path'])
    clean = project['members']['clean_slab']
    config = project['members']['configs'][0]
    reference = project['members']['gas_ref']
    _finish(clean, -100.0)
    _finish(config, -115.0)
    _finish(reference, -10.0)
    _set_actual_method(clean, 'PBE', ispin=2)
    _set_actual_method(config, 'PBE', ispin=2)
    _set_actual_method(reference, 'PBE', ispin=1)

    summary = adsorption.delta_e_rows(project)
    row = summary['rows'][0]

    assert row['delta_e'] == pytest.approx(-5.0)
    assert row['method_check']['status'] == 'unverified'
    assert not row['method_check']['issues']
    assert any('ISPIN 不一致' in warning and '人工核对' in warning
               for warning in row['method_check']['warnings'])
    assert summary['method_consistency']['status'] == 'unverified'


def test_delta_e_still_blocks_clean_slab_and_adsorption_ispin_difference(env):
    result = adsorption.create_project(
        env['tmp'] / 'spin-periodic-mismatch', 'spin-periodic-mismatch',
        clean_poscar=env['poscar']('strict-spin-slab.vasp'),
        config_poscars=[env['poscar']('strict-spin-config.vasp')],
        incar_path=env['incar'], lib_root=env['lib'])
    project = adsorption.load_project(result['project_path'])
    clean = project['members']['clean_slab']
    config = project['members']['configs'][0]
    _finish(clean, -100.0)
    _finish(config, -115.0)
    _set_actual_method(clean, 'PBE', ispin=1)
    _set_actual_method(config, 'PBE', ispin=2)

    summary = adsorption.delta_e_rows(project)
    row = summary['rows'][0]

    assert row['delta_e'] is None
    assert row['method_check']['status'] == 'incompatible'
    assert any('ISPIN 不一致' in issue for issue in row['method_check']['issues'])
    assert summary['method_consistency']['status'] == 'incompatible'


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


def test_scan_structure_files_recursive_read_only_deduplicates_and_ignores_symlinks(
        tmp_path):
    source = tmp_path / 'structures'
    nested = source / 'Li2S8_top'
    nested.mkdir(parents=True)
    poscar = nested / 'POSCAR'
    poscar.write_text('structure', encoding='utf-8')
    (source / 'S8_bridge.vasp').write_text('structure', encoding='utf-8')
    (source / 'notes.txt').write_text('ignore', encoding='utf-8')
    os.link(poscar, nested / 'zz_duplicate.poscar')
    try:
        (source / 'linked.vasp').symlink_to(poscar)
        (source / 'linked_dir').symlink_to(nested, target_is_directory=True)
    except OSError:
        pass

    before = {str(path.relative_to(source)): path.stat().st_mtime_ns
              for path in source.rglob('*') if path.is_file() and not path.is_symlink()}
    items = adsorption.scan_structure_files(source)
    after = {str(path.relative_to(source)): path.stat().st_mtime_ns
             for path in source.rglob('*') if path.is_file() and not path.is_symlink()}

    assert len(items) == 2
    assert {item['species'] for item in items} == {'Li2S8', 'S8'}
    assert all(os.path.isabs(item['path']) for item in items)
    assert before == after


def test_new_input_scan_prefers_poscar_over_stale_contcar(tmp_path):
    folder = tmp_path / 'inputs' / 'Li2S8_top'
    folder.mkdir(parents=True)
    poscar = folder / 'POSCAR'
    contcar = folder / 'CONTCAR'
    poscar.write_text(
        'new input\n1\n10 0 0\n0 10 0\n0 0 20\nC Li S\n24 2 8\nDirect\n'
        + '0 0 0\n' * 34, encoding='utf-8')
    contcar.write_text(
        'old final\n1\n10 0 0\n0 10 0\n0 0 20\nC Li S\n24 2 6\nDirect\n'
        + '0 0 0\n' * 32, encoding='utf-8')

    rows = adsorption.scan_structure_files(tmp_path / 'inputs')

    assert len(rows) == 1
    assert rows[0]['path'] == str(poscar.resolve())
    assert rows[0]['structure']['composition'] == {'C': 24, 'Li': 2, 'S': 8}
    assert '优先选 POSCAR' in rows[0]['selection_note']


def test_scan_lis_input_bundle_fills_unique_incar_clean_and_configs_read_only(tmp_path):
    root = tmp_path / 'MoS2_LiS_inputs'
    clean = root / 'clean_slab' / 'POSCAR'
    lis = root / 'adsorption' / 'Li2S8_top' / 'POSCAR'
    s8 = root / 'adsorption' / 'S8_bridge.vasp'
    clean.parent.mkdir(parents=True, exist_ok=True)
    clean.write_text(
        'clean\n1.0\n10 0 0\n0 10 0\n0 0 20\nC\n2\nDirect\n0 0 0\n.5 .5 .5\n',
        encoding='utf-8')
    lis.parent.mkdir(parents=True, exist_ok=True)
    lis.write_text(
        'Li2S8 top\n1.0\n10 0 0\n0 10 0\n0 0 20\nC Li S\n2 2 8\nDirect\n'
        + '0 0 0\n' * 12, encoding='utf-8')
    s8.parent.mkdir(parents=True, exist_ok=True)
    s8.write_text(
        'S8 bridge\n1.0\n10 0 0\n0 10 0\n0 0 20\nC S\n2 8\nDirect\n'
        + '0 0 0\n' * 10, encoding='utf-8')
    incar = root / 'INCAR'
    incar.write_text('ENCUT = 500\n', encoding='utf-8')
    before = {str(path.relative_to(root)): path.stat().st_mtime_ns
              for path in root.rglob('*') if path.is_file()}

    result = adsorption.scan_lis_input_bundle(root)

    after = {str(path.relative_to(root)): path.stat().st_mtime_ns
             for path in root.rglob('*') if path.is_file()}
    assert result['incar'] == str(incar.resolve())
    assert result['clean_slab'] == str(clean.resolve())
    assert {item['path'] for item in result['configs']} == {
        str(lis.resolve()), str(s8.resolve())}
    assert {item['species'] for item in result['configs']} == {'Li2S8', 'S8'}
    assert all(item['species_confidence'] == 'exact' for item in result['configs'])
    assert result['warnings'] == [] and result['source_read_only'] is True
    assert before == after


def test_scan_lis_input_bundle_never_guesses_ambiguous_incar_or_clean(tmp_path):
    root = tmp_path / 'bundle'
    paths = [root / 'a' / 'INCAR', root / 'b' / 'INCAR',
             root / 'clean' / 'POSCAR', root / 'bare' / 'POSCAR',
             root / 'Li2S8_slab' / 'POSCAR']
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x', encoding='utf-8')

    result = adsorption.scan_lis_input_bundle(root)

    assert result['incar'] == '' and len(result['incar_candidates']) == 2
    assert result['clean_slab'] == '' and len(result['clean_candidates']) == 2
    assert {item['species'] for item in result['configs']} == {'Li2S8'}
    assert any('手动选择' in warning for warning in result['warnings'])


def test_scan_lis_bundle_infers_generic_clean_and_groups_from_poscar_difference(tmp_path):
    root = tmp_path / 'generic-bundle'

    def write(path, elements, counts):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'structure\n1.0\n10 0 0\n0 10 0\n0 0 20\n'
            + ' '.join(elements) + '\n'
            + ' '.join(str(value) for value in counts) + '\nDirect\n'
            + '0 0 0\n' * sum(counts), encoding='utf-8')

    clean = root / '000' / 'POSCAR'
    top = root / '001' / 'POSCAR'
    bridge = root / '002' / 'POSCAR'
    write(clean, ['C'], [24])
    write(top, ['C', 'Li', 'S'], [24, 2, 8])
    write(bridge, ['C', 'Li', 'S'], [24, 2, 8])
    (root / 'INCAR').write_text('ENCUT=500\n', encoding='utf-8')

    result = adsorption.scan_lis_input_bundle(root)

    assert result['clean_slab'] == str(clean.resolve())
    assert result['clean_inference']['confidence'] == 'exact'
    assert {item['species'] for item in result['configs']} == {'Li2S8'}
    assert all(item['species_confirmed'] is True for item in result['configs'])
    assert result['species_groups'][0]['species'] == 'Li2S8'
    assert result['species_groups'][0]['count'] == 2


def test_create_project_persists_lis_reference_metadata_and_poscar_folder_labels(env):
    folder = env['tmp'] / 'Li2S8_top'
    folder.mkdir()
    config = folder / 'POSCAR'
    config.write_text(
        'ads\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
        encoding='utf-8')
    ref_job = env['tmp'] / 'refs' / 'mol_Li2S8'
    ref_job.mkdir(parents=True)
    reference_project = env['tmp'] / 'refs' / 'project.yaml'

    result = adsorption.create_project(
        env['tmp'] / 'lis-project', 'lis',
        clean_poscar=env['poscar']('slab.vasp'), config_poscars=[str(config)],
        incar_path=env['incar'], lib_root=env['lib'],
        config_species={str(config): 'Li2S8'}, species_refs={'Li2S8': -38.0},
        species_ref_jobs={'Li2S8': str(ref_job)}, molecules_dir=str(ref_job.parent),
        reference_project=str(reference_project))

    project = adsorption.load_project(result['project_path'])
    config_dir = project['members']['configs'][0]
    assert config_dir.endswith('lis_ads_Li2S8_top')
    assert project['config_species'] == {str(os.path.abspath(config_dir)): 'Li2S8'}
    assert project['species_refs'] == {'Li2S8': -38.0}
    assert project['species_ref_jobs'] == {'Li2S8': str(ref_job)}
    assert project['molecules_dir'] == str(ref_job.parent.resolve())
    assert project['reference_project'] == str(reference_project.resolve())


def test_create_project_rejects_duplicate_reference_compositions_before_writing(env):
    target = env['tmp'] / 'duplicate-reference-project'

    with pytest.raises(ValueError, match='具有相同原子组成'):
        adsorption.create_project(
            target, 'duplicate-refs',
            clean_poscar=env['poscar']('duplicate-ref-slab.vasp'),
            config_poscars=[env['poscar']('duplicate-ref-config.vasp')],
            incar_path=env['incar'], lib_root=env['lib'],
            config_species={},
            species_refs={'Li2S8': -38.0, 'S8Li2': -38.0},
            species_ref_jobs={'Li2S8': '/refs/first', 'S8Li2': '/refs/second'},
        )

    assert not target.exists()


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


def test_delta_e_prefers_normalised_explicit_config_species_mapping(env):
    """导入目录可直接叫 Li2S6；显式物种映射优先于旧的文件名后缀猜测。"""
    res = adsorption.create_project(
        env['tmp'] / 'mapped', 'mapped', clean_poscar=env['poscar']('s.vasp'),
        config_poscars=[env['poscar']('Li2S6.vasp')], incar_path=env['incar'],
        lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    cdir = proj['members']['configs'][0]
    _finish(proj['members']['clean_slab'], -100.0)
    _finish(cdir, -115.0)

    # deliberately use a relative, non-normalised key and map away from the
    # basename-inferred Li2S6 to prove explicit metadata wins.
    rel = os.path.relpath(cdir, proj['root'])
    proj['config_species'] = {os.path.join('unused', '..', rel): 'Li2S8'}
    proj['species_refs'] = {'Li2S6': -20.0, 'Li2S8': -10.0}
    ref_job = _make_species_ref_job(env['tmp'] / 'mapped-ref', energy=-10.0)
    proj['species_ref_jobs'] = {'Li2S8': str(ref_job)}

    row = adsorption.delta_e_rows(proj)['rows'][0]
    assert row['species'] == 'Li2S8'
    assert row['delta_e'] == pytest.approx(-115.0 - (-100.0) - (-10.0))


def test_delta_e_species_reference_keeps_legacy_suffix_fallback(env):
    """没有 config_species 的旧项目仍按 ``_<species>`` 后缀匹配。"""
    res = adsorption.create_project(
        env['tmp'] / 'legacy-ref', 'legacy', clean_poscar=env['poscar']('s.vasp'),
        config_poscars=[env['poscar']('Li2S4.vasp')], incar_path=env['incar'],
        lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    _finish(proj['members']['clean_slab'], -100.0)
    _finish(proj['members']['configs'][0], -115.0)
    proj['species_refs'] = {'Li2S4': -12.0}
    ref_job = _make_species_ref_job(env['tmp'] / 'legacy-ref-job', energy=-12.0)
    proj['species_ref_jobs'] = {'Li2S4': str(ref_job)}

    row = adsorption.delta_e_rows(proj)['rows'][0]
    assert row['species'] == 'Li2S4'
    assert row['delta_e'] == pytest.approx(-3.0)


def test_species_reference_summary_csv_and_row_are_auditable(env):
    res = adsorption.create_project(
        env['tmp'] / 'species-ref', 'lis', clean_poscar=env['poscar']('slab.vasp'),
        config_poscars=[env['poscar']('Li2S8.vasp')], incar_path=env['incar'],
        lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    config = proj['members']['configs'][0]
    ref_job = env['tmp'] / 'mol_Li2S8'
    _finish(proj['members']['clean_slab'], -100.0)
    _finish(config, -139.5)
    _make_species_ref_job(ref_job, energy=-38.072771, source='OSZICAR:E0')
    proj['config_species'] = {config: 'Li2S8'}
    # 缓存与 manifest 相差 0.5 μeV（容差内）；公式仍须使用 manifest 真值。
    proj['species_refs'] = {'Li2S8': -38.0727705}
    proj['species_ref_jobs'] = {'Li2S8': str(ref_job)}

    summary = adsorption.delta_e_rows(proj)
    row = summary['rows'][0]
    assert summary['has_ref'] and summary['reference_mode'] == 'species'
    assert row['reference_species'] == 'Li2S8'
    assert row['e_ref'] == pytest.approx(-38.072771)
    assert row['reference_state'] == 'DONE'
    assert row['reference_source'] == 'OSZICAR:E0'
    assert row['delta_e'] == pytest.approx(-139.5 - (-100.0) - (-38.072771))
    text = adsorption.export_csv(proj, summary, env['tmp'] / 'species.csv').read_text(
        encoding='utf-8-sig')
    assert '逐物种参考' in text and 'Li2S8=-38.072771' in text
    assert 'E(ref)=未设置' not in text


def _prepared_species_reference_project(env, *, cached=-38.072771,
                                        manifest_energy=-38.072771,
                                        state='DONE', write_manifest=True):
    result = adsorption.create_project(
        env['tmp'] / 'reference-gate-project', 'lis',
        clean_poscar=env['poscar']('gate-slab.vasp'),
        config_poscars=[env['poscar']('Li2S8-gate.vasp')],
        incar_path=env['incar'], lib_root=env['lib'])
    project = adsorption.load_project(result['project_path'])
    config = project['members']['configs'][0]
    _finish(project['members']['clean_slab'], -100.0)
    _finish(config, -139.5)
    ref_job = env['tmp'] / 'reference-gate-Li2S8'
    ref_job.mkdir()
    if write_manifest:
        _make_species_ref_job(ref_job, energy=manifest_energy, state=state)
    project['config_species'] = {config: 'Li2S8'}
    project['species_refs'] = {'Li2S8': cached}
    project['species_ref_jobs'] = {'Li2S8': str(ref_job)}
    return project


def test_species_reference_needs_human_blocks_delta(env):
    project = _prepared_species_reference_project(env, state='NEEDS_HUMAN')

    row = adsorption.delta_e_rows(project)['rows'][0]

    assert row['delta_e'] is None
    assert row['reference_state'] == 'NEEDS_HUMAN'
    assert row['e_ref'] == pytest.approx(-38.072771)  # 展示 manifest 实值但不参与公式
    assert '状态为 NEEDS_HUMAN' in row['note'] and '人工确认' in row['note']


def test_species_reference_missing_manifest_blocks_delta(env):
    project = _prepared_species_reference_project(env, write_manifest=False)

    row = adsorption.delta_e_rows(project)['rows'][0]

    assert row['delta_e'] is None and row['e_ref'] is None
    assert row['reference_state'] == '缺 job.yaml'
    assert '重新导入' in row['note'] and 'job.yaml' in row['note']


def test_species_reference_cache_drift_blocks_and_exposes_manifest_truth(env):
    project = _prepared_species_reference_project(env, cached=-38.0)

    summary = adsorption.delta_e_rows(project)
    row = summary['rows'][0]

    assert row['delta_e'] is None
    assert row['e_ref'] == pytest.approx(-38.072771)  # 不回退到 project.yaml 的 -38.0
    assert row['reference_state'] == 'DONE'
    assert '缓存与 job.yaml 不一致' in row['note'] and '容差 1e-06 eV' in row['note']
    assert summary['species_refs'] == {'Li2S8': None}
    assert summary['species_ref_cache'] == {'Li2S8': -38.0}


def test_created_species_reference_becomes_usable_after_job_finishes(env):
    project = _prepared_species_reference_project(env, cached=None)

    summary = adsorption.delta_e_rows(project)
    row = summary['rows'][0]

    assert row['reference_state'] == 'DONE'
    assert row['reference_valid'] is True
    assert row['e_ref'] == pytest.approx(-38.072771)
    assert row['delta_e'] == pytest.approx(-139.5 - (-100.0) - (-38.072771))
    evidence = summary['species_reference_evidence'][0]
    assert evidence['cache_refresh_needed'] is True
    assert '采用 DONE job.yaml 真值' in evidence['note']
    assert summary['species_refs'] == {'Li2S8': -38.072771}
    assert summary['species_ref_cache'] == {'Li2S8': None}


def test_legacy_species_reference_without_job_mapping_fails_closed(env):
    project = _prepared_species_reference_project(env)
    project.pop('species_ref_jobs')

    row = adsorption.delta_e_rows(project)['rows'][0]

    assert row['delta_e'] is None and row['reference_state'] == '未登记'
    assert '缺少 species_ref_jobs' in row['note'] and '重新选择参考项目' in row['note']


@pytest.mark.parametrize('bad_energy', [float('nan'), float('inf'), 1.0, -20000.0])
def test_delta_rejects_nonphysical_done_member_energy(env, bad_energy):
    res = adsorption.create_project(
        env['tmp'] / ('bad-' + str(len(list(env['tmp'].iterdir())))), 'bad',
        clean_poscar=env['poscar']('slab-bad.vasp'),
        config_poscars=[env['poscar']('cfg-bad.vasp')], incar_path=env['incar'],
        lib_root=env['lib'])
    proj = adsorption.load_project(res['project_path'])
    _finish(proj['members']['clean_slab'], -100.0)
    _finish(proj['members']['configs'][0], bad_energy)
    row = adsorption.delta_e_rows(proj)['rows'][0]
    assert row['delta_e'] is None and '能量缺失或不合理' in row['note']


def test_atomic_project_failure_leaves_no_target_and_same_name_can_retry(env):
    target = env['tmp'] / 'atomic-project'
    bad = env['tmp'] / 'bad.vasp'
    bad.write_text('not a POSCAR\n', encoding='utf-8')
    with pytest.raises(ValueError):
        adsorption.create_project(
            target, 'atomic', clean_poscar=str(bad),
            config_poscars=[env['poscar']('atomic-cfg.vasp')],
            incar_path=env['incar'], lib_root=env['lib'], fail_if_exists=True)
    assert not target.exists()
    result = adsorption.create_project(
        target, 'atomic', clean_poscar=env['poscar']('atomic-slab.vasp'),
        config_poscars=[env['poscar']('atomic-cfg2.vasp')],
        incar_path=env['incar'], lib_root=env['lib'], fail_if_exists=True)
    assert result['ok'] and target.is_dir()
    assert all(str(target) in path for _name, path, _warnings in result['generated'])


def test_prepared_projects_use_random_persistent_remote_instance_ids(env):
    slab = env['poscar']('uuid-slab.vasp')
    config = env['poscar']('uuid-config.vasp')
    preparation = {'request_sha256': 'a' * 64, 'inputs': {'same': True}}

    first = adsorption.create_project(
        env['tmp'] / 'uuid-project-a', 'same-name', clean_poscar=slab,
        config_poscars=[config], incar_path=env['incar'], lib_root=env['lib'],
        preparation=preparation, fail_if_exists=True)
    second = adsorption.create_project(
        env['tmp'] / 'uuid-project-b', 'same-name', clean_poscar=slab,
        config_poscars=[config], incar_path=env['incar'], lib_root=env['lib'],
        preparation=preparation, fail_if_exists=True)

    first_project = adsorption.load_project(first['project_path'])
    second_project = adsorption.load_project(second['project_path'])
    assert first_project['project_uuid'] != second_project['project_uuid']
    assert first_project['remote_namespace'] != second_project['remote_namespace']
    assert len(first_project['remote_namespace']) <= 120
    assert first_project['preparation']['project_uuid'] == first_project['project_uuid']
    for job_dir in [first_project['members']['clean_slab'],
                    *first_project['members']['configs']]:
        job = manifest.load_manifest(job_dir)
        assert job['inputs']['remote_namespace'] == first_project['remote_namespace']
