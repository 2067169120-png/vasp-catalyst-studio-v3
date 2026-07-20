"""report_full 组装测试:注入假 Origin/AI,验证 兜底/嵌图/AI 章节/降级(离线)。"""
import os

from vcstudio.project import report_full
from vcstudio.shared import manifest as mm


def _proj(tmp_path, n_done=2):
    dirs = []
    for i in range(n_done):
        d = tmp_path / f'ads_S8_c{i}'
        d.mkdir()
        m = mm.new_manifest(job_id=f'c{i}', system='s', task_type='relax',
                            calc_type='slab', inputs={})
        mm.set_state(m, 'DONE')
        m['results'] = {'energy_e0_eV': -100.0 - i}
        mm.save_manifest(d, m)
        (d / 'INCAR').write_text('ENCUT = 500\nEDIFF = 1E-5\n', encoding='utf-8')
        dirs.append(str(d))
    slab = tmp_path / 'slab'
    slab.mkdir()
    ms = mm.new_manifest(job_id='slab', system='s', task_type='relax',
                         calc_type='slab', inputs={})
    mm.set_state(ms, 'DONE')
    ms['results'] = {'energy_e0_eV': -90.0}
    mm.save_manifest(slab, ms)
    return {'name': 'proj1', 'root': str(tmp_path),
            'members': {'clean_slab': str(slab), 'gas_ref': None, 'configs': dirs}}


def test_incar_summary_reads_real_keys(tmp_path):
    p = _proj(tmp_path)
    s = report_full.incar_summary_from_dir(p['members']['configs'][0])
    assert s == {'ENCUT': 500, 'EDIFF': 1e-05}
    assert report_full.incar_summary_from_dir(tmp_path / 'nope') == {}


def test_encut_methodology_reports_consistent_mismatch_and_missing(tmp_path):
    a, b, missing = tmp_path / 'a', tmp_path / 'b', tmp_path / 'missing'
    for directory in (a, b, missing):
        directory.mkdir()
    (a / 'INCAR').write_text('ENCUT = 450\n', encoding='utf-8')
    (b / 'INCAR').write_text('ENCUT = 450\n', encoding='utf-8')

    consistent = report_full._encut_methodology_text([a, b])
    assert 'ENCUT = 450 eV 一致' in consistent
    assert '已核验 2 个成员' in consistent

    (b / 'INCAR').write_text('ENCUT = 520\n', encoding='utf-8')
    mismatch = report_full._encut_methodology_text([a, b])
    assert 'ENCUT 不一致(450、520 eV)' in mismatch
    assert '不应直接比较' in mismatch

    incomplete = report_full._encut_methodology_text([a, missing])
    assert 'ENCUT 核验不完整:1/2' in incomplete
    assert '不宣称基组一致' in incomplete


def test_generate_report_origin_ok_and_ai_ok(tmp_path):
    p = _proj(tmp_path)

    def fake_origin(specs, out_dir, opju_path=None):
        os.makedirs(out_dir, exist_ok=True)
        png = os.path.join(out_dir, 'ads_bar.png')
        open(png, 'wb').write(b'PNG')
        return {'ok': True, 'images': {'ads_bar': png}, 'error': ''}

    def fake_ai(payload):
        assert payload['computational_parameters'] == {'ENCUT': 500, 'EDIFF': 1e-05}
        return {'ok': True, 'analysis_zh': '中文解读OK', 'paragraph_en': 'English para.',
                'caveats': ['no ZPE'], 'confidence': 'high', 'error': ''}

    out = report_full.generate_project_report(
        p, tmp_path / 'r.html', origin_render=fake_origin, ai_analyze=fake_ai)
    h = out.read_text(encoding='utf-8')
    assert 'report_figs/ads_bar.png' in h            # Origin 图相对路径嵌入
    assert '中文解读OK' in h and 'English para.' in h   # AI 章节持久化
    assert 'no ZPE' in h
    # 原版章节移植(任务A收尾):统计 / 计算参数表 / 方法学约定
    assert 'ΔE 汇总统计' in h and '最强吸附' in h
    assert '计算参数' in h and 'ENCUT' in h
    assert '方法学约定' in h and '未含 ZPE/熵' in h


def test_delta_e_color_semantics(tmp_path):
    """ΔE 颜色语义(原版口径):>0 红、<-3 绿。"""
    from vcstudio.project import report
    rows = report.collect_jobs([])
    de = {'rows': [
        {'name': 'strong', 'state': 'DONE', 'e_config': -10.0, 'delta_e': -4.2, 'note': ''},
        {'name': 'bad', 'state': 'DONE', 'e_config': -1.0, 'delta_e': 0.5, 'note': ''},
        {'name': 'mid', 'state': 'DONE', 'e_config': -5.0, 'delta_e': -1.5, 'note': ''},
    ]}
    h = report.render_html(rows, report.summarize(rows), delta_e=de)
    assert h.count('#15803d;font-weight:600') == 1      # 仅 strong 绿
    assert h.count('#b91c1c;font-weight:600') == 1      # 仅 bad 红


def test_generate_report_falls_back_to_svg_and_degrades_ai(tmp_path):
    p = _proj(tmp_path)

    def dead_origin(specs, out_dir, opju_path=None):
        return {'ok': False, 'images': {}, 'error': 'no origin'}

    def no_ai(payload):
        return {'ok': False, 'error': '未配置 API key(xx)'}

    out = report_full.generate_project_report(
        p, tmp_path / 'r.html', origin_render=dead_origin, ai_analyze=no_ai)
    h = out.read_text(encoding='utf-8')
    assert '<svg' in h                                # SVG 兜底柱状图内嵌
    assert 'AI 分析未运行' in h and 'API key' in h      # AI 降级说明
    assert h.count('proj1') >= 1


def _proj_repro(tmp_path):
    """带 KPOINTS/POTCAR 溯源 + 续算历史的项目(item 6 发刊级可复现字段)。"""
    dirs = []
    for i, cname in enumerate(['ads_A', 'ads_B']):
        d = tmp_path / cname
        d.mkdir()
        m = mm.new_manifest(
            job_id=cname, system='s', task_type='relax', calc_type='slab',
            inputs={'kpoints': [3, 3, 1],
                    'potcar_provenance': [
                        {'element': 'C', 'variant': 'C',
                         'titel': 'PAW_PBE C 08Apr2002', 'enmax': 400.0},
                        {'element': 'S', 'variant': 'S',
                         'titel': 'PAW_PBE S 06Sep2000', 'enmax': 280.0}]})
        m['kpoints'] = [3, 3, 1]
        mm.set_state(m, 'DONE')
        m['results'] = {'energy_e0_eV': -100.0 - i}
        if cname == 'ads_B':                        # 一次续算历史 → 报告注"续算×1"
            m['attempts'] = [{'n': 1, 'result': 'submitted'},
                             {'n': 2, 'result': 'continued'}]
        mm.save_manifest(d, m)
        (d / 'INCAR').write_text('ENCUT = 450\nEDIFF = 1E-5\n', encoding='utf-8')
        dirs.append(str(d))
    slab = tmp_path / 'slab'
    slab.mkdir()
    ms = mm.new_manifest(job_id='slab', system='s', task_type='relax',
                         calc_type='slab', inputs={'kpoints': [3, 3, 1]})
    ms['kpoints'] = [3, 3, 1]
    mm.set_state(ms, 'DONE')
    ms['results'] = {'energy_e0_eV': -90.0}
    mm.save_manifest(slab, ms)
    (slab / 'INCAR').write_text('ENCUT = 450\nEDIFF = 1E-5\n', encoding='utf-8')
    return {'name': 'reproj', 'root': str(tmp_path),
            'members': {'clean_slab': str(slab), 'gas_ref': None, 'configs': dirs}}


def _gen_repro(tmp_path):
    p = _proj_repro(tmp_path)
    return report_full.generate_project_report(
        p, tmp_path / 'r.html',
        origin_render=lambda *a, **k: {'ok': False, 'images': {}, 'error': ''},
        ai_analyze=lambda payload: {'ok': False, 'error': 'skip'}).read_text(encoding='utf-8')


def test_report_shows_kpoints_and_potcar_provenance(tmp_path):
    h = _gen_repro(tmp_path)
    assert 'KPOINTS' in h and '3 × 3 × 1' in h            # K 点网格入计算参数表
    assert 'POTCAR provenance' in h                       # 赝势身份小表
    assert 'PAW_PBE C 08Apr2002' in h and '400' in h      # TITEL + ENMAX
    assert 'PAW_PBE S 06Sep2000' in h and '280' in h


def test_report_annotates_continuation_rounds(tmp_path):
    h = _gen_repro(tmp_path)
    assert '续算×1' in h                                  # ads_B 有一次 continued


def test_report_methodology_fixed_sentences(tmp_path):
    h = _gen_repro(tmp_path)
    assert 'BSSE' in h                                    # 平面波基组无 BSSE
    assert 'ENCUT = 450 eV 一致' in h                      # 逐成员真实 INCAR 核验
    assert '真空盒尺寸' in h                               # 气相参考盒尺寸见输入文件


def test_structure_gallery_embeds_existing_renders(tmp_path):
    p = _proj(tmp_path)
    figs = os.path.join(p['members']['configs'][0], 'figs')
    os.makedirs(figs)
    open(os.path.join(figs, 'x_top.png'), 'wb').write(b'PNG')
    out = report_full.generate_project_report(
        p, tmp_path / 'r.html',
        origin_render=lambda *a, **k: {'ok': False, 'images': {}, 'error': ''},
        ai_analyze=lambda payload: {'ok': False, 'error': 'skip'})
    h = out.read_text(encoding='utf-8')
    assert '结构图(POV-Ray)' in h and 'x_top.png' in h


def _generate_with_molecule_source(monkeypatch, tmp_path, project_dir, config_dir):
    """生成报告并捕获 freeenergy 真正收到的分子库目录。"""
    p = _proj(tmp_path)
    if project_dir is not None:
        p['molecules_dir'] = str(project_dir)
    called = {}

    def fake_path(rows, e_slab, molecules_dir, *, g_corr=None, mu_li=None,
                  managed_dirs=None, project=None):
        called['molecules_dir'] = molecules_dir
        called['managed_dirs'] = list(managed_dirs or [])
        called['project'] = project
        return None

    monkeypatch.setattr(report_full.freeenergy, 'path_from_project_and_molecules', fake_path)
    logs = []
    report_full.generate_project_report(
        p, tmp_path / 'molecule-source.html',
        config={'lis_molecules_dir': str(config_dir) if config_dir is not None else ''},
        origin_render=lambda *a, **k: {'ok': False, 'images': {}, 'error': ''},
        ai_analyze=lambda payload: {'ok': False, 'error': 'skip'}, log=logs.append)
    assert called.get('project') is p
    return called.get('molecules_dir'), logs


def test_report_prefers_imported_project_molecules_dir(monkeypatch, tmp_path):
    project_dir = tmp_path / 'imported-molecules'
    config_dir = tmp_path / 'configured-molecules'
    project_dir.mkdir()
    config_dir.mkdir()

    used, logs = _generate_with_molecule_source(
        monkeypatch, tmp_path, project_dir, config_dir)

    assert used == str(project_dir)
    assert not any('回退全局配置' in line for line in logs)


def test_report_falls_back_when_project_molecules_dir_missing(monkeypatch, tmp_path):
    missing_project_dir = tmp_path / 'moved-imported-molecules'
    config_dir = tmp_path / 'configured-molecules'
    config_dir.mkdir()

    used, logs = _generate_with_molecule_source(
        monkeypatch, tmp_path, missing_project_dir, config_dir)

    assert used == str(config_dir)
    assert any('回退全局配置' in line for line in logs)


def test_report_logs_unverified_legacy_molecule_method_warning(monkeypatch, tmp_path):
    molecules = tmp_path / 'legacy-molecules'
    molecules.mkdir()
    warning = '旧式非受管分子目录没有方法指纹，无法核验'
    monkeypatch.setattr(
        report_full.freeenergy, 'path_from_project_and_molecules',
        lambda *args, **kwargs: {'warnings': [warning]})
    logs = []
    fed = report_full._try_fed(
        {'slab': ('DONE', -10.0), 'rows': []},
        {'lis_molecules_dir': str(molecules)}, logs.append, proj={'members': {}})
    assert fed is not None
    assert any(warning in line for line in logs)


def test_member_dirs_include_species_reference_jobs_once():
    project = {'members': {
        'clean_slab': '/s', 'gas_ref': None, 'configs': ['/c'],
        'molecules': {'S8': '/m/S8'},
    }, 'species_ref_jobs': {'S8': '/m/S8', 'Li2S': '/m/Li2S'}}

    assert report_full._member_dirs(project) == ['/s', '/c', '/m/S8', '/m/Li2S']
