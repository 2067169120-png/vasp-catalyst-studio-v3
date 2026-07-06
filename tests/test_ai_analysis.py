"""LLM 分析层测试:payload/提示词纯函数 + 假 transport 调用链 + 降级路径。"""
import json

from vcstudio.project import ai_analysis as ai

_ROWS = [{'name': 'ads_S8', 'delta_e': -4.1234567},
         {'name': 'ads_Li2S4', 'delta_e': -2.71},
         {'name': 'bad_cfg', 'delta_e': None}]


def test_build_payload_real_params_and_numbers():
    p = ai.build_payload(project_name='ZnTa', delta_rows=_ROWS,
                         incar_summary={'ENCUT': 500, 'EDIFF': 1e-5},
                         kpoints=[3, 3, 1])
    assert p['adsorption_energies_eV']['ads_S8'] == -4.1235      # 数字非字符串
    assert p['computational_parameters'] == {'ENCUT': 500, 'EDIFF': 1e-5}  # 真实参数
    assert p['incomplete'] == ['bad_cfg'] and p['kpoints'] == [3, 3, 1]
    assert 'negative = favorable' in p['sign_convention']


def test_prompt_and_system_guardrails():
    prompt = ai.build_prompt(ai.build_payload(project_name='p', delta_rows=_ROWS,
                                              incar_summary={}))
    assert 'no invented references' in prompt
    assert 'ads_S8' in prompt
    for guard in ('NEVER invent literature', 'no ZPE/entropy', 'more negative = stronger',
                  'analysis_zh', 'paragraph_en', 'caveats'):
        assert guard in ai.SYSTEM_PROMPT


def test_export_md_contains_both_prompts():
    md = ai.render_export_md(ai.build_payload(project_name='p', delta_rows=_ROWS,
                                              incar_summary={}))
    assert md.startswith('# VASP 结果分析提示词包')
    assert 'System prompt' in md and 'User prompt' in md and 'ads_S8' in md


def test_analyze_happy_with_fake_transport():
    seen = {}

    def fake(url, body, headers, timeout):
        seen['url'] = url
        seen['body'] = json.loads(body)
        reply = {'choices': [{'message': {'content': json.dumps({
            'analysis_zh': '吸附强度 S8 最强', 'paragraph_en': 'The adsorption...',
            'caveats': ['no ZPE'], 'confidence': 'high'})}}]}
        return 200, json.dumps(reply).encode()

    out = ai.analyze({'x': 1}, api_key='k', transport=fake)
    assert out['ok'] and out['analysis_zh'] == '吸附强度 S8 最强'
    assert out['caveats'] == ['no ZPE'] and out['confidence'] == 'high'
    assert seen['body']['temperature'] == 0.2                    # 低温事实性
    assert seen['body']['messages'][0]['role'] == 'system'


def test_analyze_no_key_degrades(monkeypatch):
    monkeypatch.setattr(ai, 'load_api_key', lambda: None)   # 隔离 keyring(机器上可能存了真 key)
    out = ai.analyze({'x': 1}, api_key=None,
                     transport=lambda *a: (_ for _ in ()).throw(AssertionError))
    assert not out['ok'] and 'API key' in out['error']


def test_analyze_http_error_retries_then_fails():
    calls = []

    def bad(url, body, headers, timeout):
        calls.append(1)
        return 429, b'rate limited'

    out = ai.analyze({'x': 1}, api_key='k', transport=bad)
    assert not out['ok'] and 'HTTP 429' in out['error'] and len(calls) == 2  # 重试一次


def test_analyze_bad_json_degrades():
    def weird(url, body, headers, timeout):
        return 200, b'{"choices": [{"message": {"content": "not json"}}]}'

    out = ai.analyze({'x': 1}, api_key='k', transport=weird)
    assert not out['ok'] and '解析失败' in out['error']
