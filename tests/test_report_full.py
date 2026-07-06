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
