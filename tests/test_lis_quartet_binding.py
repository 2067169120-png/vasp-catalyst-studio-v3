"""Atomic same-directory quartet binding for Li-S adsorption inputs."""
from __future__ import annotations

from pathlib import Path

import pytest

from vcstudio.project import adsorption
from vcstudio.shared import manifest


def _poscar(elements_counts):
    elements = list(elements_counts)
    counts = [elements_counts[element] for element in elements]
    coordinates = ''.join('0 0 0\n' for _ in range(sum(counts)))
    return (
        'member\n1.0\n10 0 0\n0 10 0\n0 0 20\n'
        + ' '.join(elements) + '\n'
        + ' '.join(str(value) for value in counts) + '\n'
        + 'Direct\n' + coordinates
    )


def _potcar(elements):
    return ''.join(
        f'TITEL = PAW_PBE {element} 01Jan2001\n'
        'ENMAX = 300.0; ENMIN = 200.0\n'
        for element in elements
    )


def _write_quartet(folder: Path, elements_counts, *, nsw=20):
    folder.mkdir(parents=True)
    files = {
        'POSCAR': _poscar(elements_counts),
        'INCAR': f'# member-local settings\nENCUT = 500\nNSW = {nsw}\nISMEAR = 0\n',
        'KPOINTS': 'member mesh\n0\nGamma\n2 2 1\n0 0 0\n',
        'POTCAR': _potcar(elements_counts),
    }
    for name, content in files.items():
        (folder / name).write_text(content, encoding='utf-8')
    return {name: folder / name for name in files}


def _write_partial(folder: Path, elements_counts, *, nsw=20):
    folder.mkdir(parents=True)
    (folder / 'POSCAR').write_text(_poscar(elements_counts), encoding='utf-8')
    (folder / 'INCAR').write_text(
        f'ENCUT = 500\nNSW = {nsw}\nISMEAR = 0\n', encoding='utf-8')


def _library(root: Path, elements=('C', 'Li', 'S')):
    for element in elements:
        folder = root / element
        folder.mkdir(parents=True)
        (folder / 'POTCAR').write_text(
            f'TITEL = PAW_PBE {element} 01Jan2001\n'
            'ENMAX = 300.0; ENMIN = 200.0\n', encoding='utf-8')
    return root


def _by_path(scan):
    return {item['path']: item for item in scan['structures']}


def _source_evidence(item):
    return {
        name.lower(): {'path': record['path'], 'sha256': record['sha256']}
        for name, record in item['quartet']['files'].items()
    }


def _disable_registries(monkeypatch):
    monkeypatch.setattr(adsorption.ledger, 'register', lambda _path: None)
    monkeypatch.setattr(adsorption, 'register_project', lambda _path: True)


def test_scan_returns_read_only_quartet_mode_files_and_sha(tmp_path):
    clean = _write_quartet(tmp_path / 'inputs' / 'clean_slab', {'C': 1})
    config = _write_quartet(
        tmp_path / 'inputs' / 'Li2S8_top', {'C': 1, 'Li': 2, 'S': 8}, nsw=80)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in [*clean.values(), *config.values()]
    }

    scan = adsorption.scan_lis_input_bundle(
        tmp_path / 'inputs', reference_species=['Li2S8'])

    rows = _by_path(scan)
    for poscar in (clean['POSCAR'], config['POSCAR']):
        item = rows[str(poscar.resolve())]
        assert item['mode'] == 'copy'
        assert item['quartet']['status'] == 'ready'
        assert item['files'] == item['quartet']['files']
        assert set(item['files']) == {'POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'}
        for record in item['files'].values():
            assert record['sha256'] == manifest.sha256_file(record['path'])
    assert scan['source_read_only'] is True
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in [*clean.values(), *config.values()]
    }


def test_create_project_copies_complete_quartets_byte_for_byte_and_audits_hashes(
        tmp_path, monkeypatch):
    _disable_registries(monkeypatch)
    clean = _write_quartet(tmp_path / 'inputs' / 'clean_slab', {'C': 1})
    config = _write_quartet(
        tmp_path / 'inputs' / 'Li2S8_top', {'C': 1, 'Li': 2, 'S': 8}, nsw=80)
    scan = adsorption.scan_lis_input_bundle(
        tmp_path / 'inputs', reference_species=['Li2S8'])
    rows = _by_path(scan)
    bundles = {path: item['quartet'] for path, item in rows.items()}
    evidence = {path: _source_evidence(item) for path, item in rows.items()}
    source_before = {
        path: path.read_bytes() for path in [*clean.values(), *config.values()]
    }

    def must_not_generate(*_args, **_kwargs):
        raise AssertionError('完整四件套不应调用 build_job_dir')

    monkeypatch.setattr(adsorption, 'build_job_dir', must_not_generate)
    result = adsorption.create_project(
        tmp_path / 'managed', 'lis', clean_poscar=str(clean['POSCAR']),
        config_poscars=[str(config['POSCAR'])],
        member_input_bundles=bundles, member_source_evidence=evidence,
        config_species={str(config['POSCAR']): 'Li2S8'}, fail_if_exists=True)

    project = adsorption.load_project(result['project_path'])
    jobs_and_sources = [
        (Path(project['members']['clean_slab']), clean),
        (Path(project['members']['configs'][0]), config),
    ]
    for job_dir, sources in jobs_and_sources:
        job_manifest = manifest.load_manifest(job_dir)
        assert job_manifest['inputs']['input_mode'] == 'copy'
        assert set(job_manifest['inputs']['sha256']) == {
            'POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'}
        for name, source in sources.items():
            destination = job_dir / name
            assert destination.read_bytes() == source.read_bytes()
            assert job_manifest['inputs']['sha256'][name] == manifest.sha256_file(destination)
            assert job_manifest['inputs']['source_quartet'][name] == {
                'path': str(source.resolve()),
                'sha256': manifest.sha256_file(source),
            }
    assert source_before == {
        path: path.read_bytes() for path in [*clean.values(), *config.values()]
    }


def test_partial_member_inputs_keep_existing_generation_path(tmp_path, monkeypatch):
    _disable_registries(monkeypatch)
    _write_partial(tmp_path / 'inputs' / 'clean_slab', {'C': 1})
    _write_partial(tmp_path / 'inputs' / 'ads_top', {'C': 1}, nsw=50)
    library = _library(tmp_path / 'potcars', elements=('C',))
    scan = adsorption.scan_lis_input_bundle(tmp_path / 'inputs')
    rows = _by_path(scan)
    assert {item['mode'] for item in rows.values()} == {'generate'}
    bundles = {path: item['quartet'] for path, item in rows.items()}
    clean = str((tmp_path / 'inputs' / 'clean_slab' / 'POSCAR').resolve())
    config = str((tmp_path / 'inputs' / 'ads_top' / 'POSCAR').resolve())

    result = adsorption.create_project(
        tmp_path / 'generated', 'partial', clean_poscar=clean,
        config_poscars=[config], member_input_bundles=bundles,
        lib_root=str(library), fail_if_exists=True)

    project = adsorption.load_project(result['project_path'])
    for job_dir in [project['members']['clean_slab'], *project['members']['configs']]:
        assert all((Path(job_dir) / name).is_file()
                   for name in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'))
        assert manifest.load_manifest(job_dir)['inputs']['input_mode'] == 'generate'
    assert not (tmp_path / 'inputs' / 'clean_slab' / 'KPOINTS').exists()
    assert not (tmp_path / 'inputs' / 'ads_top' / 'POTCAR').exists()


def test_stale_quartet_hash_is_rejected_before_staging(tmp_path, monkeypatch):
    _disable_registries(monkeypatch)
    clean = _write_quartet(tmp_path / 'inputs' / 'clean_slab', {'C': 1})
    config = _write_quartet(tmp_path / 'inputs' / 'ads_top', {'C': 1}, nsw=80)
    scan = adsorption.scan_lis_input_bundle(tmp_path / 'inputs')
    rows = _by_path(scan)
    bundles = {path: item['quartet'] for path, item in rows.items()}
    config['KPOINTS'].write_text(
        'changed mesh\n0\nGamma\n1 1 1\n0 0 0\n', encoding='utf-8')
    target = tmp_path / 'must-not-exist'

    with pytest.raises(ValueError, match='源 KPOINTS.*扫描后内容已变化'):
        adsorption.create_project(
            target, 'stale', clean_poscar=str(clean['POSCAR']),
            config_poscars=[str(config['POSCAR'])],
            member_input_bundles=bundles, fail_if_exists=True)

    assert not target.exists()


def test_source_race_after_quartet_copy_rolls_back_staging(tmp_path, monkeypatch):
    _disable_registries(monkeypatch)
    clean = _write_quartet(tmp_path / 'inputs' / 'clean_slab', {'C': 1})
    config = _write_quartet(tmp_path / 'inputs' / 'ads_top', {'C': 1}, nsw=80)
    scan = adsorption.scan_lis_input_bundle(tmp_path / 'inputs')
    rows = _by_path(scan)
    bundles = {path: item['quartet'] for path, item in rows.items()}
    evidence = {path: _source_evidence(item) for path, item in rows.items()}
    real_copy2 = adsorption.shutil.copy2
    raced = {'done': False}

    def racing_copy(source, destination, *args, **kwargs):
        result = real_copy2(source, destination, *args, **kwargs)
        if Path(source).resolve() == config['POTCAR'].resolve() and not raced['done']:
            raced['done'] = True
            config['POTCAR'].write_text(
                config['POTCAR'].read_text(encoding='utf-8') + '# raced\n',
                encoding='utf-8')
        return result

    monkeypatch.setattr(adsorption.shutil, 'copy2', racing_copy)
    target = tmp_path / 'race-target'
    with pytest.raises(ValueError, match='源 POTCAR.*复制后哈希不一致'):
        adsorption.create_project(
            target, 'race', clean_poscar=str(clean['POSCAR']),
            config_poscars=[str(config['POSCAR'])],
            member_input_bundles=bundles, member_source_evidence=evidence,
            fail_if_exists=True)
    assert not target.exists()
