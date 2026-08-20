from pathlib import Path

import pytest

from vcstudio.generate import vaspsol_pair
from vcstudio.project import vaspsol
from vcstudio.shared import manifest


POSCAR = """Pt slab
1.0
3 0 0
0 3 0
0 0 15
Pt
1
Direct
0 0 0.5
"""


def _source(tmp_path: Path) -> Path:
    src = tmp_path / 'relaxed'
    src.mkdir()
    (src / 'CONTCAR').write_text(POSCAR, encoding='utf-8')
    (src / 'INCAR').write_text(
        'ENCUT = 500\nEDIFF = 1E-6\nISMEAR = 0\nSIGMA = 0.05\n', encoding='utf-8')
    (src / 'KPOINTS').write_text('Gamma\n0\nGamma\n3 3 1\n0 0 0\n', encoding='utf-8')
    (src / 'POTCAR').write_text('TITEL = PAW_PBE Pt\n', encoding='utf-8')
    m = manifest.new_manifest(
        job_id='src', system='Pt', task_type='relax', calc_type='slab', inputs={})
    manifest.set_state(m, 'DONE')
    manifest.save_manifest(src, m)
    return src


def _complete(path: str, energy: float, *, patch_evidence: bool = False) -> None:
    p = Path(path)
    (p / 'OSZICAR').write_text(f' 1 F= {energy:g} E0= {energy:g} d E =0\n', encoding='utf-8')
    footer = 'LSOL = T\nEB_K = 78.4\n' if patch_evidence else ''
    (p / 'OUTCAR').write_text(footer + 'General timing and accounting informations\n',
                              encoding='utf-8')
    m = manifest.load_manifest(p)
    manifest.set_state(m, 'DONE')
    manifest.save_manifest(p, m)


def test_build_pair_creates_two_bound_standard_jobs(tmp_path):
    result = vaspsol_pair.build_pair(str(_source(tmp_path)), eb_k=37.5)

    assert len(result['job_dirs']) == 2
    vac = manifest.load_manifest(result['vacuum_dir'])
    sol = manifest.load_manifest(result['solvent_dir'])
    assert vac['task_type'] == sol['task_type'] == 'vaspsol'
    assert vac['state'] == sol['state'] == 'CREATED'
    assert vac['vaspsol']['pair_id'] == sol['vaspsol']['pair_id'] == result['pair_id']
    assert vac['vaspsol']['role'] == 'vacuum' and sol['vaspsol']['role'] == 'solvent'
    assert 'LSOL = .FALSE.' in (Path(result['vacuum_dir']) / 'INCAR').read_text(
        encoding='utf-8')
    solvent_incar = (Path(result['solvent_dir']) / 'INCAR').read_text(
        encoding='utf-8')
    assert 'LSOL = .TRUE.' in solvent_incar and 'EB_K = 37.5' in solvent_incar


def test_pair_analysis_and_report_require_method_and_patch_evidence(tmp_path):
    pair = vaspsol_pair.build_pair(str(_source(tmp_path)), eb_k=78.4)
    _complete(pair['vacuum_dir'], -10.0)
    _complete(pair['solvent_dir'], -10.35, patch_evidence=True)

    result = vaspsol.write_report(pair['vacuum_dir'], pair['solvent_dir'])

    assert result['solvation_energy_eV'] == pytest.approx(-0.35)
    report = Path(result['report_file'])
    assert report.is_file() and 'VASPsol 真空/溶剂配对报告' in report.read_text(encoding='utf-8')
    assert result['hashes']['solvent']['OUTCAR']


def test_pair_analysis_rejects_unproven_patch_and_mismatched_method(tmp_path):
    pair = vaspsol_pair.build_pair(str(_source(tmp_path)), eb_k=78.4)
    _complete(pair['vacuum_dir'], -10.0)
    _complete(pair['solvent_dir'], -10.2)
    with pytest.raises(ValueError, match='未找到 VASPsol'):
        vaspsol.analyze_pair(pair['vacuum_dir'], pair['solvent_dir'])

    (Path(pair['solvent_dir']) / 'OUTCAR').write_text(
        'LSOL = T\nGeneral timing and accounting informations\n', encoding='utf-8')
    with (Path(pair['solvent_dir']) / 'INCAR').open('a', encoding='utf-8') as handle:
        handle.write('ENCUT = 600\n')
    with pytest.raises(ValueError, match='方法或几何不一致'):
        vaspsol.analyze_pair(pair['vacuum_dir'], pair['solvent_dir'])


def test_build_pair_refuses_non_done_source_and_existing_targets(tmp_path):
    src = _source(tmp_path)
    m = manifest.load_manifest(src)
    manifest.set_state(m, 'FAILED')
    manifest.save_manifest(src, m)
    with pytest.raises(ValueError, match='必须确认 DONE'):
        vaspsol_pair.build_pair(str(src))

    manifest.set_state(m, 'DONE')
    manifest.save_manifest(src, m)
    vaspsol_pair.build_pair(str(src))
    with pytest.raises(FileExistsError, match='拒绝覆盖'):
        vaspsol_pair.build_pair(str(src))
