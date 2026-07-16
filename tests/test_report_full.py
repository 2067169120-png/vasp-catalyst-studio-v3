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
    assert 'ENCUT' in h and '基组一致' in h                # 项目内 ENCUT 一致
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
