"""draftpack 收尾流水线测试:SI 包(清单/缺件/无 POTCAR 本体/指纹)、三线表(CSV/HTML/xlsx
兜底/缺项占位)、口径稽核六分支(一致/不一致点名)、methods_bundle(双语 + [待确认] 注入 +
不编造引文)、draft_ready 集成(通过/不通过首行醒目)。

铁律对拍:缺项一律 [待确认] 入 issues,绝不默认值冒充;POTCAR 本体绝不打包。
"""
from __future__ import annotations

import zipfile

import pytest
import yaml

from vcstudio.project import draftpack
from vcstudio.shared import manifest as mm

TS = '2026-07-17T00:00:00'


# ── 合成项目夹具 ──────────────────────────────────────────────────────────────

def _mk_member(base, name, *, energy=-100.0, state='DONE', encut=500, gga='RP',
               ispin=2, ivdw=None, kpts=(3, 3, 1), potcar=True, cont=0,
               write_files=('INCAR', 'KPOINTS', 'POSCAR', 'CONTCAR'),
               incar_text=None):
    """写一个成员作业目录(真实输入文件 + job.yaml manifest)。返回目录路径。"""
    d = base / name
    d.mkdir()
    inputs = {'poscar_sha256': 'p' * 64}
    if kpts is not None:
        inputs['kpoints'] = list(kpts)
    if potcar:
        inputs['potcar'] = [
            {'element': 'C', 'variant': 'C', 'titel': 'PAW_PBE C 08Apr2002', 'enmax': 400.0},
            {'element': 'S', 'variant': 'S', 'titel': 'PAW_PBE S 06Sep2000', 'enmax': 280.0}]
        inputs['potcar_sha256'] = 'q' * 64
    m = mm.new_manifest(job_id=name, system='sys', task_type='relax',
                        calc_type='slab', inputs=inputs)
    if kpts is not None:
        m['kpoints'] = list(kpts)
    mm.set_state(m, state)
    if state == 'DONE':
        m['results'] = {'energy_e0_eV': energy}
    if cont:
        m['attempts'] = [{'n': i + 1, 'result': 'continued'} for i in range(cont)]
    mm.save_manifest(d, m)

    if incar_text is None:
        lines = [f'ENCUT = {encut}']
        if gga is not None:
            lines.append(f'GGA = {gga}')
        if ispin is not None:
            lines.append(f'ISPIN = {ispin}')
        if ivdw is not None:
            lines.append(f'IVDW = {ivdw}')
        incar_text = '\n'.join(lines) + '\n'
    grid = ' '.join(map(str, kpts or (1, 1, 1)))
    fmap = {
        'INCAR': incar_text,
        'KPOINTS': f'auto\n0\nGamma\n{grid}\n',
        'POSCAR': f'{name}\n1.0\n10 0 0\n0 10 0\n0 0 10\nC S\n1 1\nCartesian\n0 0 0\n1 1 1\n',
        'CONTCAR': f'{name} relaxed\n1.0\n10 0 0\n0 10 0\n0 0 10\nC S\n1 1\nCartesian\n0 0 0\n1 1 1\n',
    }
    for fn in write_files:
        (d / fn).write_text(fmap[fn], encoding='utf-8')
    return str(d)


def _project(tmp_path, *, slab=None, c1=None, c2=None, gas_ref=False,
             species_refs=None):
    slab_dir = _mk_member(tmp_path, 'proj_slab_clean', energy=-90.0, **(slab or {}))
    c1_dir = _mk_member(tmp_path, 'proj_ads_Li2S4_top', energy=-100.0, cont=1, **(c1 or {}))
    c2_dir = _mk_member(tmp_path, 'proj_ads_Li2S4_fcc', energy=-99.0, **(c2 or {}))
    ref_dir = None
    if gas_ref:
        ref_dir = _mk_member(tmp_path, 'proj_ref', energy=-5.0)
    proj = {'name': 'proj', 'root': str(tmp_path),
            'members': {'clean_slab': slab_dir, 'gas_ref': ref_dir,
                        'configs': [c1_dir, c2_dir]}}
    if species_refs:
        proj['species_refs'] = species_refs
    return proj


# ══ 1. SI 装订机 ═════════════════════════════════════════════════════════════

def test_si_package_contents_list(tmp_path):
    proj = _project(tmp_path)
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    assert res['ok'] is True
    with zipfile.ZipFile(res['zip_path']) as zf:
        names = set(zf.namelist())
    for expect in ('members/proj_ads_Li2S4_top/INCAR',
                   'members/proj_ads_Li2S4_top/POSCAR',
                   'members/proj_ads_Li2S4_top/CONTCAR',
                   'members/proj_ads_Li2S4_top/job.yaml',
                   'members/proj_slab_clean/INCAR',
                   'POTCAR_identity.csv', 'energy_summary.csv',
                   'metadata.yaml', 'README.txt'):
        assert expect in names, expect


def test_si_package_excludes_potcar_body(tmp_path):
    """版权红线:POTCAR 本体绝不打包,即使目录里有 POTCAR 文件。"""
    proj = _project(tmp_path)
    # 在成员目录里放一个 POTCAR 本体,验证它不会进 zip
    import os
    open(os.path.join(proj['members']['clean_slab'], 'POTCAR'), 'w').write('PAW_PBE C ...body...')
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    with zipfile.ZipFile(res['zip_path']) as zf:
        names = zf.namelist()
    assert not any(n.endswith('/POTCAR') or n == 'POTCAR' for n in names)
    assert 'POTCAR_identity.csv' in names          # 只导出身份表


def test_si_package_potcar_identity_has_titel_enmax(tmp_path):
    proj = _project(tmp_path)
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    with zipfile.ZipFile(res['zip_path']) as zf:
        txt = zf.read('POTCAR_identity.csv').decode('utf-8-sig')
    assert 'PAW_PBE C 08Apr2002' in txt and '400.0' in txt
    assert 'PAW_PBE S 06Sep2000' in txt and '280.0' in txt


def test_si_package_missing_file_records_issue_not_fatal(tmp_path):
    """缺关键输入(POSCAR)→ issues 点名且 ok=False,但 zip 仍产出。"""
    proj = _project(tmp_path, c1={'write_files': ('INCAR', 'KPOINTS', 'CONTCAR')})  # 无 POSCAR
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    assert res['ok'] is False
    assert any('缺 POSCAR' in i and 'proj_ads_Li2S4_top' in i for i in res['issues'])
    assert zipfile.is_zipfile(res['zip_path'])     # 仍产出,不中断


def test_si_package_metadata_version_and_fingerprint(tmp_path):
    proj = _project(tmp_path)
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    with zipfile.ZipFile(res['zip_path']) as zf:
        meta = yaml.safe_load(zf.read('metadata.yaml'))
    from vcstudio import __version__
    assert meta['vcstudio_version'] == __version__
    assert meta['generated_at'] == TS
    names = {mrec['name']: mrec for mrec in meta['members']}
    assert names['proj_slab_clean']['poscar_sha256'] == 'p' * 64
    assert names['proj_slab_clean']['potcar_sha256'] == 'q' * 64
    assert names['proj_ads_Li2S4_top']['continuation_rounds'] == 1


def test_si_package_energy_summary_blocked_config_issue(tmp_path):
    """未完成构型无 ΔE → energy_summary 含真实缺口 + issues,绝不冒充数值。"""
    proj = _project(tmp_path, c2={'state': 'RUNNING'})
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    assert any('无 ΔE' in i and 'proj_ads_Li2S4_fcc' in i for i in res['issues'])


def test_si_package_readme_mentions_copyright_and_files(tmp_path):
    proj = _project(tmp_path)
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    with zipfile.ZipFile(res['zip_path']) as zf:
        readme = zf.read('README.txt').decode('utf-8')
    assert '版权' in readme and 'POTCAR' in readme
    assert 'energy_summary.csv' in readme and 'metadata.yaml' in readme
    assert '续算×1' in readme                       # 续算历史标注


def test_si_package_empty_project_not_ok(tmp_path):
    proj = {'name': 'empty', 'members': {'clean_slab': None, 'gas_ref': None, 'configs': []}}
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    assert res['ok'] is False
    assert any('无任何成员' in i for i in res['issues'])


# ══ 2. 三线表工厂 ═════════════════════════════════════════════════════════════

def test_make_tables_adsorption_csv_html_xlsx(tmp_path):
    proj = _project(tmp_path)
    res = draftpack.make_tables(proj, tmp_path / 'tables', timestamp=TS)
    names = {__import__('os').path.basename(f) for f in res['files']}
    assert {'adsorption_table.csv', 'adsorption_table.html',
            'adsorption_table.xlsx'} <= names       # openpyxl 在 → xlsx 也产


def test_make_tables_html_is_three_line(tmp_path):
    proj = _project(tmp_path)
    res = draftpack.make_tables(proj, tmp_path / 'tables', timestamp=TS)
    html = next(f for f in res['files'] if f.endswith('adsorption_table.html'))
    text = __import__('pathlib').Path(html).read_text(encoding='utf-8')
    assert 'border-top:2px solid' in text and 'border-bottom:2px solid' in text
    assert 'border-bottom:1.4px solid' in text      # 表头下细线(三线表)
    assert 'ΔE_ads/eV' in text and '是否最稳' in text and '续算轮数' in text
    assert TS in text                               # 来源脚注含传入时间戳


def test_make_tables_blocked_config_placeholder_and_issue(tmp_path):
    proj = _project(tmp_path, c2={'state': 'RUNNING'})
    res = draftpack.make_tables(proj, tmp_path / 'tables', timestamp=TS)
    csv_path = next(f for f in res['files'] if f.endswith('adsorption_table.csv'))
    text = __import__('pathlib').Path(csv_path).read_text(encoding='utf-8-sig')
    assert '[待确认]' in text                        # 缺 ΔE 占位,不留空/不冒充
    assert any('无 ΔE' in i for i in res['issues'])


def test_make_tables_free_energy_table_with_pds(tmp_path):
    proj = _project(tmp_path)
    fed = {'steps': [{'label': 'S8*', 'G': 0.0}, {'label': 'Li2S8*', 'G': -0.4},
                     {'label': 'Li2S6*', 'G': 0.3}],
           'pds_index': 1, 'u_l': -0.3, 'thermo_corrected': False}
    res = draftpack.make_tables(proj, tmp_path / 'tables', fed=fed, timestamp=TS)
    csv_path = next(f for f in res['files'] if f.endswith('free_energy_table.csv'))
    text = __import__('pathlib').Path(csv_path).read_text(encoding='utf-8-sig')
    assert 'S8*' in text and 'Li2S6*' in text and '决速步' in text
    assert 'U_L=-0.3 V' in text and '纯电子能' in text
    assert '+0.7000' in text                        # 决速步 ΔG_step = 0.3-(-0.4)


def test_make_tables_descriptor_table_reuses_screening(tmp_path):
    proj = _project(tmp_path)
    descriptors = {
        'rows': [{'catalyst': 'CoO', 'de': {'Li2S4': -1.2}, 'd_band_center': -1.5,
                  'icohp_ms': None, 'u_l': 0.3},
                 {'catalyst': 'Co9S8', 'de': {'Li2S4': -0.9}, 'd_band_center': -1.1,
                  'icohp_ms': -2.0, 'u_l': None}],
        'missing': [{'catalyst': 'CoO', 'field': 'icohp_ms', 'reason': '未提供'}]}
    res = draftpack.make_tables(proj, tmp_path / 'tables', descriptors=descriptors, timestamp=TS)
    names = {__import__('os').path.basename(f) for f in res['files']}
    assert {'descriptor_table.csv', 'descriptor_table.html'} <= names
    csv_path = next(f for f in res['files'] if f.endswith('descriptor_table.csv'))
    text = __import__('pathlib').Path(csv_path).read_text(encoding='utf-8-sig')
    assert 'CoO' in text and 'ΔE:吸附能最稳构型' in text   # screening 口径来源列
    assert any('缺 icohp_ms' in i for i in res['issues'])


def test_make_tables_without_openpyxl_falls_back_to_csv_html(tmp_path, monkeypatch):
    monkeypatch.setattr(draftpack, '_write_xlsx', lambda *a, **k: None)
    proj = _project(tmp_path)
    res = draftpack.make_tables(proj, tmp_path / 'tables', timestamp=TS)
    names = {__import__('os').path.basename(f) for f in res['files']}
    assert 'adsorption_table.csv' in names and 'adsorption_table.html' in names
    assert 'adsorption_table.xlsx' not in names     # 无 openpyxl → 不产 xlsx,但 CSV+HTML 仍在


# ══ 3. 口径稽核 ═══════════════════════════════════════════════════════════════

def test_audit_all_consistent(tmp_path):
    proj = _project(tmp_path, slab={'ivdw': 11}, c1={'ivdw': 11}, c2={'ivdw': 11})
    res = draftpack.consistency_audit(proj)
    assert res['ok'] is True
    names = {c['name'] for c in res['checks']}
    assert names == {'ENCUT 一致性', '泛函一致性', '色散校正一致性',
                     '参考态口径', 'K 网格档位记录', 'ISPIN 一致性'}
    assert '可进入投稿流程' in res['summary']


def test_audit_encut_mismatch_names_members(tmp_path):
    proj = _project(tmp_path, c1={'encut': 400})   # 与 slab/c2 的 500 不一致
    res = draftpack.consistency_audit(proj)
    enc = next(c for c in res['checks'] if c['name'] == 'ENCUT 一致性')
    assert enc['ok'] is False
    assert 'proj_ads_Li2S4_top' in enc['detail']
    assert res['ok'] is False


def test_audit_functional_mismatch_names(tmp_path):
    proj = _project(tmp_path, c1={'gga': 'PE'})    # RPBE vs PBE
    res = draftpack.consistency_audit(proj)
    fn = next(c for c in res['checks'] if c['name'] == '泛函一致性')
    assert fn['ok'] is False and 'proj_ads_Li2S4_top' in fn['detail']


def test_audit_dispersion_reuses_dispersion_audit(tmp_path):
    """部分成员设 IVDW、部分不设 → 复用 incar_builder.dispersion_audit 判不一致。"""
    proj = _project(tmp_path, slab={'ivdw': 11}, c1={'ivdw': None}, c2={'ivdw': 11})
    res = draftpack.consistency_audit(proj)
    dsp = next(c for c in res['checks'] if c['name'] == '色散校正一致性')
    assert dsp['ok'] is False
    assert '色散设置不一致' in dsp['detail'] and '成员序' in dsp['detail']


def test_audit_reference_mixed_conventions(tmp_path):
    proj = _project(tmp_path, gas_ref=True, species_refs={'Li2S4': -5.0})
    ref = next(c for c in draftpack.consistency_audit(proj)['checks']
               if c['name'] == '参考态口径')
    assert ref['ok'] is False and '混用' in ref['detail']


def test_audit_reference_none_is_surface_convention(tmp_path):
    proj = _project(tmp_path)                       # 无 gas_ref 无 species_refs
    ref = next(c for c in draftpack.consistency_audit(proj)['checks']
               if c['name'] == '参考态口径')
    assert ref['ok'] is True and 'E(slab+ads)' in ref['detail']


def test_audit_kmesh_missing_names_member(tmp_path):
    proj = _project(tmp_path, c2={'kpts': None})   # c2 manifest 无 K 网格记录
    km = next(c for c in draftpack.consistency_audit(proj)['checks']
              if c['name'] == 'K 网格档位记录')
    assert km['ok'] is False and 'proj_ads_Li2S4_fcc' in km['detail']


def test_audit_ispin_mismatch_names(tmp_path):
    proj = _project(tmp_path, c1={'ispin': 1})     # ISPIN 2 vs 1
    sp = next(c for c in draftpack.consistency_audit(proj)['checks']
              if c['name'] == 'ISPIN 一致性')
    assert sp['ok'] is False and 'proj_ads_Li2S4_top' in sp['detail']


# ══ 4. 方法学装订 ═════════════════════════════════════════════════════════════

def test_methods_bundle_writes_bilingual_and_bib(tmp_path):
    proj = _project(tmp_path)
    res = draftpack.methods_bundle(proj, tmp_path / 'methods', timestamp=TS)
    import os
    names = {os.path.basename(f) for f in res['files']}
    assert names == {'methods_zh.md', 'methods_en.md', 'references.bib'}
    from pathlib import Path
    zh = Path(next(f for f in res['files'] if f.endswith('methods_zh.md'))).read_text('utf-8')
    en = Path(next(f for f in res['files'] if f.endswith('methods_en.md'))).read_text('utf-8')
    bib = Path(next(f for f in res['files'] if f.endswith('references.bib'))).read_text('utf-8')
    assert 'VASP' in zh and 'RPBE' in zh            # 中文方法学(GGA=RP → RPBE)
    assert 'VASP' in en and 'RPBE' in en            # 英文方法学
    assert 'Kresse1996' in bib and 'Hammer1999' in bib   # VASP + RPBE 引文


def test_methods_bundle_inconsistency_injects_todo_header(tmp_path):
    """成员参数不一致 → 文本头部 [待确认] 行 + issues 点名。"""
    proj = _project(tmp_path, c1={'encut': 400})
    res = draftpack.methods_bundle(proj, tmp_path / 'methods', timestamp=TS)
    from pathlib import Path
    zh = Path(next(f for f in res['files'] if f.endswith('methods_zh.md'))).read_text('utf-8')
    assert zh.lstrip().startswith('[待确认')       # 首行醒目待确认
    assert 'ENCUT 一致性' in zh
    assert any('参数不一致' in i for i in res['issues'])


def test_methods_bundle_no_done_member(tmp_path):
    proj = _project(tmp_path, slab={'state': 'CREATED'}, c1={'state': 'CREATED'},
                    c2={'state': 'CREATED'})
    res = draftpack.methods_bundle(proj, tmp_path / 'methods', timestamp=TS)
    assert any('无 DONE 成员' in i for i in res['issues'])
    from pathlib import Path
    zh = Path(next(f for f in res['files'] if f.endswith('methods_zh.md'))).read_text('utf-8')
    assert '[待确认' in zh                           # 占位,不编造方法学


def test_methods_bundle_does_not_fabricate_citations(tmp_path):
    """无 POTCAR 身份 + INCAR 无 GGA → 泛函未知,只引 VASP,绝不编造泛函引文。"""
    proj = {'name': 'bare', 'root': str(tmp_path),
            'members': {'clean_slab':
                        _mk_member(tmp_path, 'bare_slab', energy=-90.0, potcar=False,
                                   incar_text='ENCUT = 500\n'),
                        'gas_ref': None, 'configs': []}}
    res = draftpack.methods_bundle(proj, tmp_path / 'methods', timestamp=TS)
    from pathlib import Path
    bib = Path(next(f for f in res['files'] if f.endswith('references.bib'))).read_text('utf-8')
    assert 'Kresse1996' in bib                       # VASP 恒引(真用到)
    assert 'Perdew1996' not in bib and 'Hammer1999' not in bib   # 泛函未知 → 不编造
    assert any('泛函未知' in i or 'POTCAR' in i for i in res['issues'])


# ══ 5. 一键 Draft-Ready ═══════════════════════════════════════════════════════

def test_draft_ready_all_pass(tmp_path):
    proj = _project(tmp_path, slab={'ivdw': 11}, c1={'ivdw': 11}, c2={'ivdw': 11})
    res = draftpack.draft_ready(proj, tmp_path / 'dr', timestamp=TS)
    assert res['ok'] is True
    assert set(res['report']) >= {'audit', 'si_package', 'tables', 'methods'}
    from pathlib import Path
    md = Path(res['summary_path']).read_text('utf-8')
    assert md.startswith('# ✅')                     # 通过 → 首行绿勾
    assert '可进入投稿流程' in md


def test_draft_ready_audit_fail_still_produces(tmp_path):
    """稽核不过仍产出全部工件,但 ok=False 且 DRAFT_READY.md 首行醒目。"""
    proj = _project(tmp_path, c1={'encut': 400})    # ENCUT 不一致
    res = draftpack.draft_ready(proj, tmp_path / 'dr', timestamp=TS)
    assert res['ok'] is False
    from pathlib import Path
    md = Path(res['summary_path']).read_text('utf-8')
    assert md.startswith('# ⚠️')                     # 未通过 → 首行醒目警告
    assert '进稿前必须解决' in res['summary']
    # 工件仍产出
    assert res['report']['si_package']['zip_path']
    assert res['report']['tables']['files']
    assert len(res['report']['methods']['files']) == 3


def test_draft_ready_issues_total_aggregates(tmp_path):
    proj = _project(tmp_path, c2={'state': 'RUNNING'})   # 一个构型未完成 → 若干 issue
    res = draftpack.draft_ready(proj, tmp_path / 'dr', timestamp=TS)
    assert res['issues_total'] == len(res['issues'])
    assert res['issues_total'] >= 1
    assert any('无 ΔE' in i for i in res['issues'])


def test_draft_ready_timestamp_is_injected_not_wallclock(tmp_path):
    """时间戳由调用方传入(可复现),不用 wall-clock。"""
    proj = _project(tmp_path)
    res = draftpack.draft_ready(proj, tmp_path / 'dr', timestamp=TS)
    from pathlib import Path
    assert TS in Path(res['summary_path']).read_text('utf-8')


def test_build_si_package_ok_key_present(tmp_path):
    """返回结构契约:build_si_package 恒含 ok/zip_path/contents/issues。"""
    proj = _project(tmp_path)
    res = draftpack.build_si_package(proj, tmp_path / 'SI.zip', timestamp=TS)
    assert set(res) == {'ok', 'zip_path', 'contents', 'issues'}
    assert isinstance(res['contents'], list) and isinstance(res['issues'], list)


@pytest.mark.parametrize('fn_name', ['build_si_package', 'make_tables',
                                     'consistency_audit', 'methods_bundle', 'draft_ready'])
def test_public_api_exists(fn_name):
    assert callable(getattr(draftpack, fn_name))
