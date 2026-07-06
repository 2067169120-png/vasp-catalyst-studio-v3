"""Origin 适配器测试:离线测降级/编排(假 run);真机冒烟设 VCS_ORIGIN_SMOKE=1 才跑(启动 Origin 慢)。"""
import json
import os

import pytest

from vcstudio.external import origin_charts as oc

_BAR_SPEC = {'kind': 'bar', 'name': 'ads_bar', 'title': 'E_ads',
             'data': {'rows': ['P'], 'cols': ['S8'], 'matrix': [[-4.1]],
                      'band': (-2.9, -1.65)}}


def test_degrades_without_python(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, 'find_python', lambda configured='': None)
    out = oc.render_charts([_BAR_SPEC], str(tmp_path))
    assert not out['ok'] and 'Python' in out['error']


def test_render_charts_orchestration(tmp_path):
    """假 run:验证 runner/spec 落盘、命令形状、结果 JSON 读回、缺图过滤。"""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        png = os.path.join(str(tmp_path), 'ads_bar.png')
        with open(png, 'wb') as f:
            f.write(b'PNG')
        with open(os.path.join(str(tmp_path), '_origin_result.json'), 'w', encoding='utf-8') as f:
            json.dump({'images': {'ads_bar': png, 'ghost': png + '.missing'},
                       'errors': ['heatmap: numpy 缺']}, f)

        class P:
            returncode = 0
        return P()

    out = oc.render_charts([_BAR_SPEC], str(tmp_path), python_exe='py.exe', run=fake_run)
    assert out['ok'] and list(out['images']) == ['ads_bar']    # 不存在的文件被过滤
    assert 'numpy' in out['error']
    assert calls[0][0] == 'py.exe' and calls[0][1].endswith('_origin_runner.py')
    spec = json.load(open(tmp_path / '_origin_spec.json', encoding='utf-8'))
    assert spec['charts'][0]['kind'] == 'bar' and spec['width'] == 2400
    runner = (tmp_path / '_origin_runner.py').read_text(encoding='utf-8')
    assert 'originpro' in runner and 'render_bar' in runner


def test_render_charts_no_result_reports_stderr(tmp_path):
    def fake_run(cmd, **kw):
        class P:
            returncode = 1
            stderr = b'ImportError: originpro'
        return P()

    out = oc.render_charts([_BAR_SPEC], str(tmp_path), python_exe='py.exe', run=fake_run)
    assert not out['ok'] and 'originpro' in out['error']


def test_stale_result_removed(tmp_path):
    """上一轮 result.json 必须被清掉,防止本轮失败却读到旧结果误报成功。"""
    stale = tmp_path / '_origin_result.json'
    stale.write_text('{"images": {"old": "x.png"}}', encoding='utf-8')

    def fake_run(cmd, **kw):
        class P:
            returncode = 1
            stderr = b''
        return P()

    out = oc.render_charts([_BAR_SPEC], str(tmp_path), python_exe='py.exe', run=fake_run)
    assert not out['ok'] and 'old' not in str(out['images'])


@pytest.mark.skipif(os.environ.get('VCS_ORIGIN_SMOKE') != '1',
                    reason='真机 Origin 冒烟需 VCS_ORIGIN_SMOKE=1(启动 Origin 较慢)')
def test_real_origin_smoke(tmp_path):
    """真机:真启动 Origin 渲柱状图,验证非空 PNG。"""
    out = oc.render_charts([_BAR_SPEC], str(tmp_path), width=1200, timeout=300)
    assert out['ok'], out['error']
    assert os.path.getsize(out['images']['ads_bar']) > 5000
