"""job.yaml manifest 测试:round-trip、溯源、状态机、与 build_job_dir 的集成。"""
import pytest

from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate import task_catalog
from vcstudio.shared import manifest


def _make_fixture(tmp_path):
    """最小可生成环境:单元素 C 的假 POTCAR 库 + 单原子 POSCAR(同 test_incar_builder)。"""
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   TITEL  = PAW_PBE C 08Apr2002\n'
        '   ENMAX  =  273.214; ENMIN = 200.000 eV\n',
        encoding='utf-8')
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(
        'C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
        encoding='utf-8')
    return str(poscar), str(lib)


def test_create_from_build_writes_job_yaml(tmp_path):
    poscar, lib = _make_fixture(tmp_path)
    out = tmp_path / 'job1'
    res = build_job_dir(poscar, 'ISMEAR = 0\n', str(out),
                        calc_type='molecule', lib_root=lib)
    m = manifest.create_from_build(str(out), res, poscar_path=poscar, validate=True)

    assert (out / 'job.yaml').is_file()
    loaded = manifest.load_manifest(str(out))
    assert loaded is not None
    assert loaded['schema'] == manifest.SCHEMA_VERSION
    assert loaded['state'] == 'CREATED'
    assert loaded['job_id'] == m['job_id']
    # 溯源:sha256 与 POSCAR 实文件一致;体系名取 POSCAR 首行
    assert loaded['inputs']['poscar_sha256'] == manifest.sha256_file(poscar)
    assert loaded['system'] == 'C atom'
    # 集成:calc_type / kpoints / elements 从 build 结果透传
    assert loaded['calc_type'] == 'molecule'
    assert loaded['inputs']['kpoints'] == [1, 1, 1]
    assert loaded['inputs']['elements'] == ['C']
    # 缺 ENCUT → 有补全项 → 来源标记 user+completion
    assert 'ENCUT' in loaded['inputs']['completions']
    assert loaded['inputs']['incar_source'] == 'user+completion'
    # 旧调用方未传 incar_path 时保持兼容，不伪造源路径。
    assert 'source_incar_path' not in loaded['inputs']
    assert 'source_incar_sha256' not in loaded['inputs']


def test_create_from_build_records_source_and_final_managed_input_hashes(tmp_path):
    """源 INCAR 与受管目录最终输入分开溯源；自动补全会使两者哈希不同。"""
    poscar, lib = _make_fixture(tmp_path)
    source_incar = tmp_path / 'member-a' / 'INCAR'
    source_incar.parent.mkdir()
    source_incar.write_text('ISMEAR = 0\n', encoding='utf-8')
    out = tmp_path / 'managed' / 'job-a'
    res = build_job_dir(poscar, str(source_incar), str(out),
                        calc_type='molecule', lib_root=lib)

    manifest.create_from_build(
        str(out), res, poscar_path=poscar, incar_path=source_incar, validate=True)
    inputs = manifest.load_manifest(out)['inputs']

    assert inputs['source_incar_path'] == str(source_incar.resolve())
    assert inputs['source_incar_sha256'] == manifest.sha256_file(source_incar)
    assert set(inputs['sha256']) == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    assert inputs['sha256'] == {
        name: manifest.sha256_file(out / name)
        for name in ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')
    }
    assert inputs['sha256']['INCAR'] != inputs['source_incar_sha256']


def test_create_from_build_hashes_only_existing_managed_inputs(tmp_path):
    poscar, lib = _make_fixture(tmp_path)
    out = tmp_path / 'partial-audit'
    res = build_job_dir(poscar, 'ENCUT = 400\n', str(out),
                        calc_type='molecule', lib_root=lib)
    (out / 'KPOINTS').unlink()

    manifest.create_from_build(str(out), res, poscar_path=poscar)

    hashes = manifest.load_manifest(out)['inputs']['sha256']
    assert set(hashes) == {'INCAR', 'POSCAR', 'POTCAR'}


def test_manifest_records_potcar_provenance(tmp_path):
    """发刊级溯源(审查#2):赝势身份(variant/TITEL/ENMAX)+ POTCAR sha256 落入 job.yaml。"""
    poscar, lib = _make_fixture(tmp_path)
    out = tmp_path / 'job2'
    res = build_job_dir(poscar, 'ENCUT = 400\n', str(out),
                        calc_type='molecule', lib_root=lib)
    manifest.create_from_build(str(out), res, poscar_path=poscar, validate=True)
    loaded = manifest.load_manifest(str(out))
    pv = loaded['inputs']['potcar']
    assert pv == [{'element': 'C', 'variant': 'C',
                   'titel': 'PAW_PBE C 08Apr2002', 'enmax': 273.214}]
    assert loaded['inputs']['potcar_sha256'] == manifest.sha256_file(out / 'POTCAR')


def test_incar_source_labels():
    assert manifest.incar_source_label(False, {}) == 'user_no_validate'
    assert manifest.incar_source_label(True, {}) == 'user_verbatim'
    assert manifest.incar_source_label(True, {'ENCUT': 400}) == 'user+completion'


def test_manifest_task_types_cover_full_catalog_and_normalize_legacy_aliases():
    catalog_keys = tuple(item['key'] for item in task_catalog.CATALOG)
    assert len(catalog_keys) == 23
    assert catalog_keys == manifest.CATALOG_TASK_TYPES
    assert set(catalog_keys).issubset(manifest.KNOWN_TASK_TYPES)
    assert manifest.normalize_task_type('band') == 'bands'
    assert manifest.normalize_task_type('DOS') == 'dos_pdos'
    m = manifest.new_manifest(
        job_id='legacy-band', system='s', task_type='band', calc_type='bulk', inputs={})
    assert m['task_type'] == 'bands'


def test_unknown_task_type_is_rejected_instead_of_silently_becoming_relax(tmp_path):
    with pytest.raises(ValueError, match='未知任务类型'):
        manifest.new_manifest(
            job_id='bad', system='s', task_type='statci', calc_type='bulk', inputs={})

    poscar, lib = _make_fixture(tmp_path)
    out = tmp_path / 'bad-build'
    res = build_job_dir(poscar, 'ENCUT = 400\n', str(out),
                        calc_type='molecule', lib_root=lib)
    res['task_type'] = 'statci'
    with pytest.raises(ValueError, match='未知任务类型'):
        manifest.create_from_build(str(out), res, poscar_path=poscar)
    assert not (out / 'job.yaml').exists()


def test_set_state_appends_history_and_rejects_bogus(tmp_path):
    poscar, lib = _make_fixture(tmp_path)
    out = tmp_path / 'job2'
    res = build_job_dir(poscar, 'ENCUT = 400\n', str(out),
                        calc_type='molecule', lib_root=lib)
    m = manifest.create_from_build(str(out), res, poscar_path=poscar, validate=True)

    manifest.set_state(m, 'SUBMITTED', note='qsub 8812345')
    assert m['state'] == 'SUBMITTED'
    assert m['state_history'][-1]['state'] == 'SUBMITTED'
    assert m['state_history'][-1]['note'] == 'qsub 8812345'
    assert [h['state'] for h in m['state_history']] == ['CREATED', 'SUBMITTED']

    with pytest.raises(ValueError, match='非法作业状态'):
        manifest.set_state(m, 'WHATEVER')


def test_save_is_atomic_no_tmp_left(tmp_path):
    poscar, lib = _make_fixture(tmp_path)
    out = tmp_path / 'job3'
    res = build_job_dir(poscar, 'ENCUT = 400\n', str(out),
                        calc_type='molecule', lib_root=lib)
    manifest.create_from_build(str(out), res, poscar_path=poscar, validate=True)
    leftovers = list(out.glob('*.tmp'))
    assert leftovers == []


def test_load_missing_returns_none(tmp_path):
    assert manifest.load_manifest(str(tmp_path)) is None
